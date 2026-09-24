from __future__ import annotations

from typing import Any

from ..models import Decision, GameAction
from .base import ProviderError


class MockProvider:
    """Deterministic local adapter for interface demonstrations; never calls the game API."""

    def __init__(self, name: str = "mock"):
        self.name = name

    async def choose(
        self, *, state: dict[str, Any], actions: list[GameAction], min_confidence: float
    ) -> Decision:
        del state
        if not actions:
            raise ProviderError("Mock provider received no actions.")
        confidence = 0.99
        if confidence < min_confidence:
            raise ProviderError("Mock confidence is below the configured threshold.")
        return Decision(action_id=actions[0].action_id, provider=self.name, confidence=confidence,
                        reason="Deterministic mock selected the first supplied legal candidate.")

    async def plan_route(self, *, state: dict[str, Any], min_confidence: float) -> dict[str, Any]:
        del min_confidence
        map_data = state.get("map", {})
        start = map_data.get("current_position", {})
        nodes = map_data.get("nodes", [])
        options = map_data.get("next_options", [])
        bosses = map_data.get("bosses", [])
        boss = map_data.get("boss") or (bosses[0] if isinstance(bosses, list) and bosses else None)
        if not isinstance(start, dict) or not isinstance(boss, dict) or not isinstance(nodes, list) or not isinstance(options, list):
            raise ProviderError("Mock route needs current_position, next_options, nodes, and boss data.")
        start_key = (start.get("col"), start.get("row"))
        boss_key = (boss.get("col"), boss.get("row"))
        if any(not isinstance(v, int) for v in (*start_key, *boss_key)):
            raise ProviderError("Mock route received invalid start or boss coordinates.")
        graph: dict[tuple[int, int], list[tuple[int, int]]] = {}
        types: dict[tuple[int, int], str] = {}
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("col"), int) or not isinstance(node.get("row"), int):
                continue
            key = (node["col"], node["row"])
            types[key] = str(node.get("type", "unknown"))
            children = node.get("children", [])
            graph[key] = [(pair[0], pair[1]) for pair in children if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair)] if isinstance(children, list) else []
        starts = [(n.get("col"), n.get("row")) for n in options if isinstance(n, dict) and isinstance(n.get("col"), int) and isinstance(n.get("row"), int)]
        from collections import deque
        queue = deque((key, [key]) for key in starts)
        seen = set(starts)
        found: list[tuple[int, int]] | None = None
        while queue:
            key, path = queue.popleft()
            if key == boss_key:
                found = path
                break
            for child in graph.get(key, []):
                if child not in seen:
                    seen.add(child)
                    queue.append((child, path + [child]))
        if found is None:
            raise ProviderError("Mock could not find a route from the visible choices to the boss.")
        return {"route": [{"col": col, "row": row} for col, row in found], "confidence": 0.99,
                "reason": "Mock-only breadth-first route for plumbing demonstration; not a game strategy recommendation."}
