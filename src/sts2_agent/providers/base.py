from __future__ import annotations

from typing import Any, Protocol

from ..models import Decision, GameAction


class DecisionProvider(Protocol):
    name: str

    async def choose(
        self, *, state: dict[str, Any], actions: list[GameAction], min_confidence: float
    ) -> Decision: ...


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    """Transient provider transport/server failure before any game action was sent."""


class LowConfidenceError(ProviderError):
    """A valid candidate distribution whose selectivity is below the configured gate."""

    def __init__(
        self,
        message: str,
        *,
        confidence: float,
        action_id: str | None = None,
        probabilities: dict[str, float] | None = None,
        route: list[dict[str, int]] | None = None,
    ) -> None:
        super().__init__(message)
        self.confidence = confidence
        self.action_id = action_id
        self.probabilities = probabilities or {}
        self.route = route or []


class ProviderConfigurationError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass
