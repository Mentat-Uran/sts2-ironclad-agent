from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def fingerprint(value: Any) -> str:
    packed = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class GameAction:
    action_id: str
    action: str
    phase: str
    description: str
    payload: dict[str, Any] = field(default_factory=dict)
    strategic: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action": self.action,
            "phase": self.phase,
            "description": self.description,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class GameSnapshot:
    state_type: str
    raw: dict[str, Any]
    state_fingerprint: str
    game_version: str | None
    character: str | None
    complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    action_id: str
    provider: str
    confidence: float
    reason: str
    probabilities: dict[str, float] = field(default_factory=dict)
    state_fingerprint: str = ""
    luna_context_update: dict[str, Any] = field(default_factory=dict)
    plan_steps: tuple[dict[str, Any], ...] = ()
    line_choice_id: str | None = None
    line_probabilities: dict[str, float] = field(default_factory=dict)


class DecisionPaused(RuntimeError):
    """A deliberate stop caused by uncertain, stale, incomplete or illegal state."""
