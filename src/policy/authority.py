from __future__ import annotations
"""Authority engine — the section 2 / section 3 boundary, read from data.

WHY THIS MODULE EXISTS
----------------------
Policy ACA-2026/1 section 1 says: "An assistant that is technically capable of
an action it is not permitted to take is not compliant, however carefully it has
been instructed." The obvious way to satisfy that is to write the boundary into
the flow of the agent -- `if requested == "suspend": escalate()`. That works
until the policy changes, which the brief promises it will, and then compliance
is a code change and a deploy.

So the boundary lives in `data/policy/authority-rules.json` and this module only
knows how to *read* it. There is no provision number, no permitted verb and no
restricted verb hard-coded below. Adding section 3.9 tomorrow is a data edit.

THE DRIFT PROBLEM
-----------------
A machine-readable projection of a prose policy has one dangerous failure mode:
somebody edits the prose and not the projection. The engine then enforces a
boundary that no longer exists, silently, and every run looks fine.

`load()` closes that by requiring every rule to carry a `quote` lifted verbatim
from the prose, and refusing to load if the quote is not there. Editing the
policy text without updating the rules is a startup failure, not a silent
divergence. This is the only mechanism in the project that can detect a policy
change nobody told the code about.

DEFAULT DENY
------------
Section 2 is a closed list. Anything it does not name is either named by section
3 or is unclear -- and 6.1 sends unclear to section 3. So the resolution order
is: section 2, then section 3, then 6.1. Silence in the policy is a refusal.
"""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from src.observability.logging_setup import get_logger, log_event

logger = get_logger(__name__)


class PolicyLoadError(RuntimeError):
    """The policy could not be loaded in a state safe to enforce.

    Raised rather than degraded on purpose. A half-loaded authority boundary is
    worse than no run: it produces confident decisions from an unknown rule set.
    """


class Authority(str, Enum):
    """What the policy says about an action kind."""
    PERMITTED = "permitted"
    REQUIRES_APPROVAL = "requires_approval"
    #: Named by neither list. Not a third category in the policy -- 6.1 maps it
    #: onto requires_approval -- but tracked separately so the trace can show
    #: that the assistant was interpreting rather than citing.
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9\s]+")


def normalise(text: Any) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Applied identically to rule phrases and to referral text, so
    "Review award", "review of the award." and "REVIEW  AWARD" all behave the
    same. Hyphens become spaces, which is why "Counter-Fraud Unit" matches the
    phrase "counter-fraud".
    """
    if text is None:
        return ""
    lowered = str(text).lower()
    stripped = _PUNCT.sub(" ", lowered)
    return _WS.sub(" ", stripped).strip()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchSpec:
    """Phrase matcher for one rule. `none` is a veto and is checked last."""
    any_of: tuple[str, ...] = ()
    all_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "MatchSpec":
        raw = raw or {}
        return cls(
            any_of=tuple(normalise(p) for p in raw.get("any", []) if str(p).strip()),
            all_of=tuple(normalise(p) for p in raw.get("all", []) if str(p).strip()),
            none_of=tuple(normalise(p) for p in raw.get("none", []) if str(p).strip()),
        )

    @property
    def empty(self) -> bool:
        return not (self.any_of or self.all_of)

    def match(self, normalised_text: str) -> Optional[str]:
        """Return the phrase that matched, or None.

        The returned phrase goes into the audit trail: a supervisor reading
        "matched on 'review award'" can check the reasoning without re-running
        anything.
        """
        if self.empty or not normalised_text:
            return None
        for phrase in self.none_of:
            if phrase and phrase in normalised_text:
                return None
        for phrase in self.all_of:
            if phrase not in normalised_text:
                return None
        if self.any_of:
            hits = [p for p in self.any_of if p in normalised_text]
            if not hits:
                return None
            # Longest match wins, so a specific phrase is reported in preference
            # to a substring of itself.
            return max(hits, key=len)
        return self.all_of[0] if self.all_of else None


@dataclass(frozen=True)
class Rule:
    """One provision of the policy, as the engine sees it."""
    id: str
    provision: str
    section: str
    effect: str
    quote: str
    label: str = ""
    action_kind: str = ""
    match_spec: MatchSpec = field(default_factory=MatchSpec)
    subject_matter: MatchSpec = field(default_factory=MatchSpec)
    performable: bool = False
    no_preparatory_version: str = ""
    supervisor_action: str = ""
    why_irreversible: str = ""
    interpretation_applied: str = ""
    notes: tuple[str, ...] = ()
    #: Only the default rule uses this. 6.1 is an interpretation rule, not a
    #: prohibition: when it fires the action is treated as falling within
    #: section 3, so the determination cites the section named here and records
    #: 6.1 as how it got there.
    attributed_provision: str = ""

    @property
    def requires_approval(self) -> bool:
        return self.effect == Authority.REQUIRES_APPROVAL.value

    @property
    def cited_provision(self) -> str:
        return self.attributed_provision or self.provision

    @classmethod
    def from_dict(cls, raw: dict, *, default_section: str) -> "Rule":
        notes = raw.get("notes", ())
        if isinstance(notes, str):
            notes = (notes,)
        return cls(
            id=str(raw.get("id") or raw.get("provision") or ""),
            provision=str(raw.get("provision") or raw.get("id") or ""),
            section=str(raw.get("section") or default_section),
            effect=str(raw.get("effect") or ""),
            quote=str(raw.get("quote") or ""),
            label=str(raw.get("label") or ""),
            action_kind=str(raw.get("action_kind") or ""),
            match_spec=MatchSpec.from_dict(raw.get("match")),
            subject_matter=MatchSpec.from_dict(raw.get("subject_matter")),
            performable=bool(raw.get("performable", False)),
            no_preparatory_version=str(raw.get("no_preparatory_version") or ""),
            supervisor_action=str(raw.get("supervisor_action") or ""),
            why_irreversible=str(raw.get("why_irreversible") or ""),
            interpretation_applied=str(raw.get("interpretation_applied") or ""),
            notes=tuple(str(n) for n in notes),
            attributed_provision=str(raw.get("attributed_provision") or ""),
        )

    def citation(self) -> str:
        return f"section {self.provision}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provision": self.provision,
            "section": self.section,
            "effect": self.effect,
            "label": self.label,
            "action_kind": self.action_kind,
            "quote": self.quote,
            "performable": self.performable,
            "supervisor_action": self.supervisor_action,
            "no_preparatory_version": self.no_preparatory_version,
            "why_irreversible": self.why_irreversible,
        }


@dataclass
class RelatedMatter:
    """A section 3 provision engaged by the case content rather than the ask.

    RF-2026-0422 asks for a triage note, which section 2.4 permits. The case is
    an Appeals Panel decision reinstating an award, which is section 3.2. Doing
    only what was asked and saying nothing about the reinstatement would satisfy
    the letter of the request and leave the actual restricted action unflagged.
    Section 2.7 exists precisely so the assistant can name it.
    """
    provision: str
    quote: str
    label: str
    matched_phrase: str
    matched_in: str            # which field carried the phrase
    supervisor_action: str = ""
    action_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provision": self.provision,
            "quote": self.quote,
            "label": self.label,
            "matched_phrase": self.matched_phrase,
            "matched_in": self.matched_in,
            "supervisor_action": self.supervisor_action,
            "action_kind": self.action_kind,
        }


@dataclass
class AuthorityDetermination:
    """The policy's answer about one requested action.

    Everything a supervisor needs to check the reasoning is on this object:
    which provision, the provision's own words, the phrase that triggered it,
    and whether 6.1 was applied because the policy was unclear rather than
    explicit. That last field matters: the data pack says applying 6.1 and
    saying so is a good answer, and an audit trail that cannot distinguish a
    citation from an interpretation hides the difference.
    """
    requested_action: str
    authority: Authority
    provision: str
    section: str
    quote: str
    label: str
    action_kind: str
    rationale: str
    matched_phrase: str = ""
    interpretation_applied: str = ""
    supervisor_action: str = ""
    no_preparatory_version: str = ""
    why_irreversible: str = ""
    policy_ref: str = ""
    rules_version: str = ""
    related_restricted: list[RelatedMatter] = field(default_factory=list)
    notes: tuple[str, ...] = ()

    @property
    def permitted(self) -> bool:
        return self.authority is Authority.PERMITTED

    @property
    def must_escalate(self) -> bool:
        """True if anything about this referral needs a supervisor.

        Either the requested action itself is restricted, or the case content
        engages a section 3 provision the request did not mention.
        """
        return not self.permitted or bool(self.related_restricted)

    @property
    def escalated_provisions(self) -> list[str]:
        provisions = [] if self.permitted else [self.provision]
        provisions += [m.provision for m in self.related_restricted]
        return provisions

    def one_line(self) -> str:
        verb = "PERMITTED" if self.permitted else "REQUIRES SUPERVISOR APPROVAL"
        tail = f" (via {self.interpretation_applied})" if self.interpretation_applied else ""
        return f"{verb} under section {self.provision}{tail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_action": self.requested_action,
            "authority": self.authority.value,
            "permitted": self.permitted,
            "must_escalate": self.must_escalate,
            "provision": self.provision,
            "section": self.section,
            "quote": self.quote,
            "label": self.label,
            "action_kind": self.action_kind,
            "rationale": self.rationale,
            "matched_phrase": self.matched_phrase,
            "interpretation_applied": self.interpretation_applied,
            "supervisor_action": self.supervisor_action,
            "no_preparatory_version": self.no_preparatory_version,
            "why_irreversible": self.why_irreversible,
            "policy_ref": self.policy_ref,
            "rules_version": self.rules_version,
            "escalated_provisions": self.escalated_provisions,
            "related_restricted": [m.to_dict() for m in self.related_restricted],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

@dataclass
class AuthorityPolicy:
    """Policy ACA-2026/1, loaded from data and verified against its own prose."""

    policy_ref: str
    policy_title: str
    issuing_body: str
    in_force_from: str
    rules_version: str
    rules_path: str
    source_path: str
    source_text: str
    permitted: list[Rule]
    restricted: list[Rule]
    default_rule: Rule
    interpretation: list[Rule]
    escalation: list[Rule]
    traceability: list[Rule]

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(
        cls,
        rules_path: str = "data/policy/authority-rules.json",
        source_path: Optional[str] = None,
        *,
        verify_quotes: bool = True,
    ) -> "AuthorityPolicy":
        """Load the rules and check them against the prose they claim to encode.

        Args:
            rules_path: the machine-readable decision table.
            source_path: the prose policy. Defaults to whatever the rules file
                names in `source_document`, so the rules point at their own
                source rather than the caller guessing.
            verify_quotes: leave this on. It is the drift check.
        """
        rules_file = Path(rules_path)
        if not rules_file.exists():
            raise PolicyLoadError(
                f"Authority policy rules not found at {rules_path}. The assistant "
                f"has no authority boundary to enforce and will not run. "
                f"Expected the file copied from the data pack into this repository."
            )
        try:
            raw = json.loads(rules_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PolicyLoadError(f"{rules_path} is not valid JSON: {exc}") from exc

        source = source_path or raw.get("source_document") or ""
        source_file = Path(source)
        if not source_file.exists():
            raise PolicyLoadError(
                f"The rules in {rules_path} cite {source or '(unset)'} as their "
                f"source document, and it is not there. The quotes cannot be "
                f"verified, so the boundary cannot be trusted."
            )
        source_text = source_file.read_text(encoding="utf-8")
        haystack = normalise(source_text)

        def build(key: str, section: str, *, required: bool = False) -> list[Rule]:
            entries = raw.get(key) or []
            if required and not entries:
                raise PolicyLoadError(f"{rules_path} has no '{key}' rules.")
            return [Rule.from_dict(r, default_section=section) for r in entries]

        permitted = build("permitted", "2", required=True)
        restricted = build("restricted", "3", required=True)
        interpretation = build("interpretation", "6")
        escalation = build("escalation", "4")
        traceability = build("traceability", "5")

        default_raw = raw.get("default_rule")
        if not default_raw:
            raise PolicyLoadError(
                f"{rules_path} has no `default_rule`. Without it an unrecognised "
                f"request has no defined outcome, and the boundary would be "
                f"fail-open. Section 6.1 must be expressed here."
            )
        default_rule = Rule.from_dict(default_raw, default_section="6")

        every_rule = (
            permitted + restricted + [default_rule]
            + interpretation + escalation + traceability
        )

        if verify_quotes:
            missing = [
                f"section {r.provision}: {r.quote[:70]!r}"
                for r in every_rule
                if not r.quote or normalise(r.quote) not in haystack
            ]
            if missing:
                raise PolicyLoadError(
                    "Policy drift detected. These rules quote text that is not in "
                    f"{source}:\n  - " + "\n  - ".join(missing) +
                    "\n\nThe prose policy and the machine-readable rules have "
                    "diverged. Refusing to run: the assistant would be enforcing "
                    "a boundary the policy no longer states. Reconcile "
                    f"{rules_path} with {source} and try again."
                )

        # Structural checks that would otherwise surface as a wrong decision.
        problems: list[str] = []
        for rule in permitted:
            if rule.effect != Authority.PERMITTED.value:
                problems.append(f"section {rule.provision} is listed under 'permitted' "
                                f"but its effect is {rule.effect!r}")
            if not rule.performable:
                problems.append(f"section {rule.provision} is permitted but not marked "
                                f"performable, so nothing would ever run it")
        for rule in restricted:
            if rule.effect != Authority.REQUIRES_APPROVAL.value:
                problems.append(f"section {rule.provision} is listed under 'restricted' "
                                f"but its effect is {rule.effect!r}")
            if rule.performable:
                problems.append(
                    f"section {rule.provision} requires approval but is marked "
                    f"performable. That would bind an effect function to a "
                    f"restricted action and break the structural gate."
                )
        kinds: dict[str, str] = {}
        for rule in permitted + restricted:
            if not rule.action_kind:
                problems.append(f"section {rule.provision} has no action_kind")
                continue
            if rule.action_kind in kinds:
                problems.append(
                    f"action_kind {rule.action_kind!r} is claimed by both section "
                    f"{kinds[rule.action_kind]} and section {rule.provision}. One "
                    f"kind, one authority -- otherwise the answer depends on "
                    f"iteration order."
                )
            kinds[rule.action_kind] = rule.provision
        if default_rule.effect != Authority.REQUIRES_APPROVAL.value:
            problems.append(
                f"default_rule effect is {default_rule.effect!r}. The default must "
                f"be requires_approval or the boundary is fail-open."
            )
        if problems:
            raise PolicyLoadError(
                f"{rules_path} is internally inconsistent:\n  - "
                + "\n  - ".join(problems)
            )

        policy = cls(
            policy_ref=str(raw.get("policy_ref") or "unknown"),
            policy_title=str(raw.get("policy_title") or ""),
            issuing_body=str(raw.get("issuing_body") or ""),
            in_force_from=str(raw.get("in_force_from") or ""),
            rules_version=str(raw.get("rules_version") or "0"),
            rules_path=str(rules_path),
            source_path=str(source),
            source_text=source_text,
            permitted=permitted,
            restricted=restricted,
            default_rule=default_rule,
            interpretation=interpretation,
            escalation=escalation,
            traceability=traceability,
        )
        log_event(
            logger, "policy.loaded",
            policy_ref=policy.policy_ref, rules_version=policy.rules_version,
            permitted_provisions=[r.provision for r in permitted],
            restricted_provisions=[r.provision for r in restricted],
            quotes_verified=verify_quotes,
        )
        return policy

    # -- lookups ------------------------------------------------------------

    def all_rules(self) -> list[Rule]:
        return self.permitted + self.restricted

    def rule_for_kind(self, action_kind: str) -> Optional[Rule]:
        for rule in self.all_rules():
            if rule.action_kind == action_kind:
                return rule
        return None

    def rule_for_provision(self, provision: str) -> Optional[Rule]:
        for rule in (self.all_rules() + [self.default_rule]
                     + self.interpretation + self.escalation + self.traceability):
            if rule.provision == provision:
                return rule
        return None

    def authority_for_kind(self, action_kind: str) -> Authority:
        """What the policy says about an action kind the assistant wants to take.

        UNKNOWN for a kind the policy does not name. Callers treat UNKNOWN as
        refusal -- 6.1 -- but it is reported distinctly so the trace can say
        "the policy does not mention this" rather than inventing a citation.
        """
        rule = self.rule_for_kind(action_kind)
        if rule is None:
            return Authority.UNKNOWN
        return Authority(rule.effect)

    def known_action_kinds(self) -> frozenset[str]:
        """Every action kind the policy names, permitted or not.

        This is the allowlist the action-type validator uses. It comes from data,
        so a new kind added to the policy is recognised without a code change --
        and a kind in neither the policy nor the code is unknown, and fails
        closed.
        """
        return frozenset(r.action_kind for r in self.all_rules() if r.action_kind)

    def performable_kinds(self) -> frozenset[str]:
        """Kinds an effect function may be bound to. Section 2 only."""
        return frozenset(
            r.action_kind for r in self.permitted if r.action_kind and r.performable
        )

    def restricted_kinds(self) -> frozenset[str]:
        """Kinds the assistant must never perform. Section 3."""
        return frozenset(r.action_kind for r in self.restricted if r.action_kind)

    # -- the decision -------------------------------------------------------

    def determine(
        self,
        requested_action: str,
        *,
        summary: str = "",
        source: str = "",
        extra_context: str = "",
    ) -> AuthorityDetermination:
        """Decide whether the assistant may carry out `requested_action`.

        Resolution order is section 2, then section 3, then 6.1. Section 2 is
        checked first on purpose: the act of drafting a triage note is permitted
        even when the case it concerns is one where a section 3 action is owed.
        The section 3 action does not disappear -- it comes back as a related
        restricted matter, which is what 2.7 is for.

        Note what is NOT an input here: urgency, confidence, how serious the
        summary sounds, or whether a caseworker would probably approve. Section
        6.2 makes those irrelevant, so they are not passed in.
        """
        asked = normalise(requested_action)

        for rule in self.permitted:
            phrase = rule.match_spec.match(asked)
            if phrase:
                determination = AuthorityDetermination(
                    requested_action=requested_action,
                    authority=Authority.PERMITTED,
                    provision=rule.provision,
                    section=rule.section,
                    quote=rule.quote,
                    label=rule.label,
                    action_kind=rule.action_kind,
                    matched_phrase=phrase,
                    rationale=(
                        f"Section {rule.provision} permits this without approval: "
                        f"\"{rule.quote}\" The request matched on {phrase!r}."
                    ),
                    interpretation_applied=rule.interpretation_applied,
                    policy_ref=self.policy_ref,
                    rules_version=self.rules_version,
                    notes=rule.notes,
                )
                break
        else:
            for rule in self.restricted:
                phrase = rule.match_spec.match(asked)
                if phrase:
                    determination = AuthorityDetermination(
                        requested_action=requested_action,
                        authority=Authority.REQUIRES_APPROVAL,
                        provision=rule.provision,
                        section=rule.section,
                        quote=rule.quote,
                        label=rule.label,
                        action_kind=rule.action_kind,
                        matched_phrase=phrase,
                        rationale=(
                            f"Section {rule.provision} requires supervisor approval "
                            f"before this action is taken: \"{rule.quote}\" The "
                            f"request matched on {phrase!r}."
                            + (f" Section {rule.interpretation_applied} was applied: "
                               f"it is arguable that this request is not literally a "
                               f"section {rule.provision} action, and where that is "
                               f"unclear the policy requires treating it as though "
                               f"it is." if rule.interpretation_applied else "")
                        ),
                        interpretation_applied=rule.interpretation_applied,
                        supervisor_action=rule.supervisor_action,
                        no_preparatory_version=rule.no_preparatory_version,
                        why_irreversible=rule.why_irreversible,
                        policy_ref=self.policy_ref,
                        rules_version=self.rules_version,
                        notes=rule.notes,
                    )
                    break
            else:
                rule = self.default_rule
                determination = AuthorityDetermination(
                    requested_action=requested_action,
                    authority=Authority.REQUIRES_APPROVAL,
                    provision=rule.cited_provision,
                    section="3",
                    quote=rule.quote,
                    label=rule.label or "Unclear whether section 3 applies",
                    action_kind=rule.action_kind or "unclassified_request",
                    matched_phrase="",
                    rationale=(
                        f"The request {requested_action!r} is not among the acts "
                        f"section 2 permits, and no specific section 3 provision "
                        f"names it. Section 6.1 applies: \"{rule.quote}\" It is "
                        f"therefore treated as requiring supervisor approval."
                    ),
                    interpretation_applied="6.1",
                    supervisor_action=rule.supervisor_action,
                    policy_ref=self.policy_ref,
                    rules_version=self.rules_version,
                    notes=rule.notes,
                )

        determination.related_restricted = self._scan_subject_matter(
            primary_provision=(
                determination.provision if not determination.permitted else ""
            ),
            fields={
                "requested_action": requested_action,
                "summary": summary,
                "source": source,
                "context": extra_context,
            },
        )

        log_event(
            logger, "policy.determined",
            requested_action=requested_action,
            authority=determination.authority.value,
            provision=determination.provision,
            interpretation_applied=determination.interpretation_applied or None,
            matched_phrase=determination.matched_phrase or None,
            related=[m.provision for m in determination.related_restricted],
        )
        return determination

    def _scan_subject_matter(
        self, *, primary_provision: str, fields: dict[str, str],
    ) -> list[RelatedMatter]:
        """Find section 3 provisions engaged by the case, not by the request.

        Runs over the referral's own words. A referral can ask for something
        permitted while describing something restricted; the assistant is
        required by 2.7 to identify that and escalate it.
        """
        found: list[RelatedMatter] = []
        seen = {primary_provision} if primary_provision else set()

        for rule in self.restricted:
            if rule.subject_matter.empty or rule.provision in seen:
                continue
            for field_name, value in fields.items():
                phrase = rule.subject_matter.match(normalise(value))
                if not phrase:
                    continue
                found.append(RelatedMatter(
                    provision=rule.provision,
                    quote=rule.quote,
                    label=rule.label,
                    matched_phrase=phrase,
                    matched_in=field_name,
                    supervisor_action=rule.supervisor_action,
                    action_kind=rule.action_kind,
                ))
                seen.add(rule.provision)
                break
        return found

    # -- introspection for the CLI and the console -------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "policy_ref": self.policy_ref,
            "policy_title": self.policy_title,
            "issuing_body": self.issuing_body,
            "in_force_from": self.in_force_from,
            "rules_version": self.rules_version,
            "rules_path": self.rules_path,
            "source_path": self.source_path,
            "permitted": [r.to_dict() for r in self.permitted],
            "restricted": [r.to_dict() for r in self.restricted],
            "default_rule": self.default_rule.to_dict(),
            "escalation": [
                {"provision": r.provision, "quote": r.quote} for r in self.escalation
            ],
            "traceability": [
                {"provision": r.provision, "quote": r.quote} for r in self.traceability
            ],
            "interpretation": [
                {"provision": r.provision, "quote": r.quote} for r in self.interpretation
            ],
            "performable_kinds": sorted(self.performable_kinds()),
            "restricted_kinds": sorted(self.restricted_kinds()),
        }


_cached: dict[tuple[str, str], AuthorityPolicy] = {}


def load_policy(
    rules_path: str = "data/policy/authority-rules.json",
    source_path: Optional[str] = None,
) -> AuthorityPolicy:
    """Load and cache the policy for a process.

    Cached because the quote verification reads and normalises the whole prose
    document, and every task would otherwise repeat it per referral.
    """
    key = (str(rules_path), str(source_path or ""))
    if key not in _cached:
        _cached[key] = AuthorityPolicy.load(rules_path, source_path)
    return _cached[key]


def reset_policy_cache() -> None:
    """Drop the cached policy. For tests, and for `serve` reloading rules."""
    _cached.clear()
