"""Effects — the only code path from a decision to a side effect.

`registry.py` holds the structural approval gate: effects may only be bound to
action kinds the authority policy marks performable, so the eight section 3 kinds
have no callable and cannot acquire one. `permitted.py` implements the seven
section 2 acts that do.
"""

from src.effects.permitted import build_permitted_effects
from src.effects.registry import (
    ActionNotPerformable,
    EffectBindingError,
    EffectError,
    EffectFn,
    EffectOutcome,
    EffectRegistry,
    EffectRequest,
    RegistryReport,
)

__all__ = [
    "ActionNotPerformable",
    "EffectBindingError",
    "EffectError",
    "EffectFn",
    "EffectOutcome",
    "EffectRegistry",
    "EffectRequest",
    "RegistryReport",
    "build_permitted_effects",
]
