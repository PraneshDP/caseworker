"""Policy — the authority boundary, loaded from data rather than written in code.

`authority.py` reads `data/policy/authority-rules.json`, verifies every rule
against the prose it quotes in `data/policy/authority-policy.md`, and answers one
question: may the assistant do this on its own, or does a supervisor decide?
"""

from src.policy.authority import (
    Authority,
    AuthorityDetermination,
    AuthorityPolicy,
    PolicyLoadError,
    RelatedMatter,
    Rule,
    load_policy,
    normalise,
    reset_policy_cache,
)

__all__ = [
    "Authority",
    "AuthorityDetermination",
    "AuthorityPolicy",
    "PolicyLoadError",
    "RelatedMatter",
    "Rule",
    "load_policy",
    "normalise",
    "reset_policy_cache",
]
