from __future__ import annotations

import asyncio
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from ..models import Decision, GameAction
from ..strategy_memory import normalize_luna_context_update
from .base import LowConfidenceError, ProviderConfigurationError, ProviderError, ProviderResponseError, RetryableProviderError


@lru_cache(maxsize=1)
def _luna_ironclad_skill() -> str:
    skill_path = Path(__file__).resolve().parents[3] / ".agents" / "skills" / "sts2-ironclad-luna" / "SKILL.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderConfigurationError(f"GPT-6 Luna Skill could not be loaded from {skill_path}.") from exc


class LunaChatCompletionsProvider:
    """GPT-6 Luna over the explicitly configured OpenAI Chat Completions endpoint."""

    name = "gpt-6-luna"

    def __init__(self, base_url: str, model: str, api_key: str | None, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = httpx.Timeout(timeout_seconds)

    async def choose(
        self, *, state: dict[str, Any], actions: list[GameAction], min_confidence: float
    ) -> Decision:
        if not self.api_key:
            raise ProviderConfigurationError("GPT-6 Luna is not configured: set the API key in the local STS2_LUNA_API_KEY environment variable.")
        if not actions:
            raise ProviderResponseError("GPT-6 Luna received no legal actions.")
        action_table = [action.public() for action in actions]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a Slay the Spire 2 Ironclad strategy selector. Choose one action only from the supplied legal action IDs. "
                    "Return one JSON object with exactly: action_id (string), confidence (number from 0 to 1), reason (short string), luna_context_update (object). "
                    "luna_context_update is a bounded strategy patch with optional keys: archetype_hypothesis, win_condition, supporting_signals, missing_roles, upgrade_priorities [{card_id,reason}], combat_rules, avoid. "
                    "Always return luna_context_update; use {} when this choice adds no useful run-level insight. Update only what changed or is newly supported. "
                    "Treat luna_context.confirmed_run_items as confirmed from accepted game actions; treat luna_context.strategy as a hypothesis. Never invent owned cards/relics or override current live HP, deck, hand, energy, intents, targets, offered options, or legal actions. "
                    "For card_reward, skip is the default. Compare every offered card against the full live player.deck and choose a card only when it has a clear run-specific advantage over adding nothing; a merely best-of-three or duplicate role is not enough. At 20 or more cards, require a clearly strong answer to an upcoming threat, a real uncovered role, or a payoff for an engine already in the deck. The short reason must name the missing role or existing engine and why that benefit beats deck dilution; otherwise choose the legal skip action. "
                    "For shops, leave-shop is a valid strategic choice: do not spend just because gold is available. Reassess each purchase against the live deck, route and post-purchase gold; preserve at least 40 gold as a soft reserve unless the purchase directly addresses an urgent near-term survival or major deck gap. Never spend down to a near-zero balance to exhaust inventory. The short reason must state the remaining gold and the immediate payoff when crossing below the reserve. Follow the attached Luna-only Ironclad Skill for pick priors and exceptions. "
                    "Do not invent actions or indices. If the supplied state is insufficient, return action_id as an empty string and confidence 0."
                    "\n\nLUNA-ONLY IRONCLAD SKILL (never send this text to KEV):\n"
                    + _luna_ironclad_skill()
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"state": state, "legal_actions": action_table}, ensure_ascii=False),
            },
        ]
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError("GPT-6 Luna request timed out; the game action was not submitted.") from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"GPT-6 Luna endpoint unavailable ({type(exc).__name__}); the game action was not submitted.") from exc
        if response.status_code == 401:
            raise ProviderConfigurationError("GPT-6 Luna rejected authentication (HTTP 401); paste the correct key locally and retry.")
        if response.status_code >= 400:
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableProviderError(
                    f"GPT-6 Luna returned transient HTTP {response.status_code}; the game action was not submitted."
                )
            raise ProviderError(f"GPT-6 Luna returned HTTP {response.status_code}; the game action was not submitted.")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            action_id = parsed["action_id"]
            confidence = float(parsed["confidence"])
            reason = str(parsed.get("reason", ""))
            luna_context_update_raw = parsed["luna_context_update"]
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ProviderResponseError("GPT-6 Luna response was not valid Chat Completions JSON in the required decision schema.") from exc
        if not isinstance(luna_context_update_raw, dict):
            raise ProviderResponseError("GPT-6 Luna luna_context_update must be a JSON object.")
        legal_ids = {action.action_id for action in actions}
        if not isinstance(action_id, str) or action_id not in legal_ids:
            raise ProviderResponseError("GPT-6 Luna selected an ID outside the current legal action set.")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ProviderResponseError("GPT-6 Luna returned an invalid confidence value.")
        if confidence < min_confidence:
            raise LowConfidenceError(
                f"GPT-6 Luna confidence {confidence:.3f} is below the configured {min_confidence:.3f} threshold; pausing.",
                confidence=confidence,
                action_id=action_id,
            )
        return Decision(
            action_id=action_id, provider=self.name, confidence=confidence, reason=reason[:500],
            luna_context_update=normalize_luna_context_update(luna_context_update_raw),
        )

    async def plan_route(self, *, state: dict[str, Any], min_confidence: float) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderConfigurationError("GPT-6 Luna is not configured: set the API key in the local STS2_LUNA_API_KEY environment variable.")
        messages = [
            {
                "role": "system",
                "content": (
                    "Plan one complete legal route for a Slay the Spire 2 Ironclad run from the current map position to the listed boss. "
                    "The route array contains only future nodes: NEVER include map.current_position. "
                    "The first point must be exactly one item from map.next_options. Every later point must follow the previous node's children edge. "
                    "Use coordinates exactly as supplied: col is column and row is floor. Finish with a coordinate listed as map.boss or map.bosses. "
                    "Return one JSON object with route (array of {col,row}), confidence (0..1), reason, and luna_context_update (object). "
                    "luna_context_update uses only the bounded strategy-patch keys archetype_hypothesis, win_condition, supporting_signals, missing_roles, upgrade_priorities, combat_rules, and avoid. Always include it; return {} if there is no new strategic insight. Never claim unobserved cards/relics are owned. "
                    "Example: if current_position is {col:3,row:0}, next_options contains {col:0,row:1}, the route starts [{col:0,row:1}, ...], not with {col:3,row:0}. "
                    "Use player.deck, relics, HP, gold and luna_context as supplied strategic information; never invent a deck when it is absent. "
                    "Do not invent nodes or claim a route is certain. If the state is insufficient, return an empty route and confidence 0."
                    "\n\nLUNA-ONLY IRONCLAD SKILL (never send this text to KEV):\n"
                    + _luna_ironclad_skill()
                ),
            },
            {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
        ]
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": self.model, "messages": messages, "temperature": 0,
                          "response_format": {"type": "json_object"}},
                )
                if response.status_code in {500, 502, 503, 504}:
                    await asyncio.sleep(0.25)
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json={"model": self.model, "messages": messages, "temperature": 0,
                              "response_format": {"type": "json_object"}},
                    )
        except httpx.TimeoutException as exc:
            raise ProviderError("GPT-6 Luna route-planning request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"GPT-6 Luna route-planning endpoint unavailable ({type(exc).__name__}).") from exc
        if response.status_code == 401:
            raise ProviderConfigurationError("GPT-6 Luna rejected authentication (HTTP 401); paste the correct key locally and retry.")
        if response.status_code >= 400:
            raise ProviderError(f"GPT-6 Luna returned HTTP {response.status_code} while planning a route.")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            route = parsed["route"]
            confidence = float(parsed["confidence"])
            reason = str(parsed.get("reason", ""))
            luna_context_update_raw = parsed["luna_context_update"]
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ProviderResponseError("GPT-6 Luna route response did not match the required JSON schema.") from exc
        if not isinstance(luna_context_update_raw, dict):
            raise ProviderResponseError("GPT-6 Luna route luna_context_update must be a JSON object.")
        if not isinstance(route, list) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ProviderResponseError("GPT-6 Luna returned an invalid route or confidence value.")
        if confidence < min_confidence:
            safe_route = [
                {"col": item["col"], "row": item["row"]}
                for item in route
                if isinstance(item, dict)
                and isinstance(item.get("col"), int)
                and isinstance(item.get("row"), int)
            ]
            raise LowConfidenceError(
                f"GPT-6 Luna route confidence {confidence:.3f} is below the configured {min_confidence:.3f} threshold; pausing.",
                confidence=confidence,
                route=safe_route,
            )
        coords: list[dict[str, int]] = []
        for item in route:
            if not isinstance(item, dict) or not isinstance(item.get("col"), int) or not isinstance(item.get("row"), int):
                raise ProviderResponseError("GPT-6 Luna route contains a node without integer col and row coordinates.")
            coords.append({"col": item["col"], "row": item["row"]})
        return {
            "route": coords, "confidence": confidence, "reason": reason[:500],
            "luna_context_update": normalize_luna_context_update(luna_context_update_raw),
        }
