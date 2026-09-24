from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter
from dataclasses import replace
from typing import Any
from uuid import uuid4

import httpx

from .actions import derive_actions
from .combat_tactics import combat_candidate_policy, combat_pressure, emergency_defense_override
from .config import Settings, read_local_game_version
from .game_api import GameApi, GameApiError
from .models import Decision, DecisionPaused, GameAction, GameSnapshot, fingerprint
from .providers import KevStructuredProvider, LunaChatCompletionsProvider, MockProvider
from .providers.base import (
    LowConfidenceError, ProviderConfigurationError, ProviderError,
    ProviderResponseError, RetryableProviderError,
)
from .state import COMBAT_SCREENS, normalize_state
from .telemetry import AgentEventLog

PASSIVE_BRIDGE_SCHEMA = 2
TRANSIENT_COMBAT_STATE_ERRORS = {
    "Combat is not in a ready player action phase.",
    "Combat state is missing player or battle data.",
    "Combat state is missing hand or enemy data.",
}
TERMINAL_SCREEN_TYPES = {
    "victory", "run_complete", "campaign_complete", "defeat", "death",
    "game_over", "run_over",
}


class Controller:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.game = GameApi(settings.game_base_url, settings.game_timeout_seconds)
        self.mock = MockProvider()
        self.kev = KevStructuredProvider(
            settings.kev_base_url, settings.kev_model, settings.kev_timeout_seconds,
            settings.kev_permutation_average,
        )
        self.luna = LunaChatCompletionsProvider(
            settings.luna_base_url, settings.luna_model, settings.luna_api_key,
            settings.luna_timeout_seconds,
        )
        self._routes: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._autoplay_route_id: str | None = None
        self._autoplay_next_snapshot: GameSnapshot | None = None
        self._card_selection_key: str | None = None
        self._selected_card_indices: set[int] = set()
        self._last_autoplay_submission: dict[str, Any] | None = None
        self._planned_combat_line: dict[str, Any] | None = None
        self._start_profile_id: int | None = None
        self._start_ironclad_selected = False
        self._last_menu_action_checkpoint: dict[str, str] | None = None
        self._last_action_at = 0.0
        self.events = AgentEventLog()

    async def snapshot(self) -> GameSnapshot:
        raw = await self._read_passive_bridge_state()
        version = read_local_game_version(self.settings.game_dir)
        snapshot = normalize_state(raw, version)
        errors = list(snapshot.errors)
        if self.settings.expected_game_version and version != self.settings.expected_game_version:
            errors.append(
                f"Game version is {version or 'unknown'}, expected {self.settings.expected_game_version}; refresh compatibility evidence before acting."
            )
        if snapshot.character and self.settings.require_ironclad:
            character = snapshot.character.casefold()
            if character not in {"ironclad", "the ironclad", "铁甲战士"}:
                errors.append(f"This project is restricted to Ironclad; current character is {snapshot.character!r}.")
        elif self.settings.require_ironclad and snapshot.state_type in {
            "monster", "elite", "boss", "hand_select", "rewards", "card_reward", "map",
            "event", "rest_site", "shop", "fake_merchant", "treasure", "card_select",
            "bundle_select", "relic_select", "crystal_sphere",
        }:
            errors.append("The active run does not identify its character; Ironclad-only mode is paused.")
        if tuple(errors) != snapshot.errors:
            snapshot = replace(snapshot, complete=False, errors=tuple(errors))
        return snapshot

    async def _read_passive_bridge_state(self) -> dict[str, Any]:
        # Older Mod builds mutate UI while producing a snapshot (for example, they
        # open shop inventory and treasure chests). Check the harmless root hello
        # before requesting any game state.
        hello = await self.game.ping()
        if hello.get("status") != "ok" or hello.get("bridge_schema_version") != PASSIVE_BRIDGE_SCHEMA:
            raise GameApiError(
                "Unsupported game bridge: passive state schema 2 is required; "
                "the bridge was not asked to read game state."
            )
        raw = await self.game.read_state()
        if raw.get("bridge_schema_version") != PASSIVE_BRIDGE_SCHEMA:
            raise GameApiError(
                "Game bridge hello advertised schema 2 but the state payload did not; "
                "the state is rejected."
            )
        return raw

    async def legal_actions(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        actions, catalog_errors = derive_actions(snapshot)
        return {
            "state_type": snapshot.state_type,
            "game_version": snapshot.game_version,
            "character": snapshot.character,
            "state_fingerprint": snapshot.state_fingerprint,
            "complete": snapshot.complete and not catalog_errors,
            "errors": [*snapshot.errors, *catalog_errors],
            "legal_actions": [action.public() for action in actions],
        }

    async def preflight(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "game": {"status": "unknown", "base_url": self.settings.game_base_url},
            "kev": {"status": "unknown", "base_url": self.settings.kev_base_url, "model": self.settings.kev_model},
            "luna": {
                "status": "key_missing" if not self.settings.luna_api_key else "checking",
                "base_url": self.settings.luna_base_url,
                "model": self.settings.luna_model,
                "key_configured": bool(self.settings.luna_api_key),
                "key_sent": False,
            },
            "game_actions_enabled": self.settings.game_actions_enabled,
        }
        try:
            hello = await self.game.ping()
            result["game"]["bridge"] = hello
            if hello.get("status") != "ok" or hello.get("bridge_schema_version") != PASSIVE_BRIDGE_SCHEMA:
                result["game"].update({
                    "status": "unsupported_bridge",
                    "error": "Passive state schema 2 is required; no game-state request was sent.",
                })
            else:
                raw_state = await self.game.read_state()
                if raw_state.get("bridge_schema_version") != PASSIVE_BRIDGE_SCHEMA:
                    result["game"].update({
                        "status": "unsupported_bridge",
                        "error": "Bridge hello and state schema do not match; state was rejected.",
                    })
                else:
                    result["game"].update({"status": "ok", "state_type": raw_state.get("state_type")})
        except GameApiError as exc:
            result["game"].update({"status": "error", "error": str(exc)})
        game_version = read_local_game_version(self.settings.game_dir)
        result["game"]["local_version"] = game_version
        result["game"]["expected_version"] = self.settings.expected_game_version
        result["game"]["version_compatible"] = (
            game_version == self.settings.expected_game_version if self.settings.expected_game_version else None
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.kev_timeout_seconds), trust_env=False) as client:
                response = await client.get(f"{self.settings.kev_base_url}/v1/models")
            response.raise_for_status()
            model_data = response.json()
            models = model_data.get("models", [])
            match = next((model for model in models if model.get("id") == self.settings.kev_model), None)
            if match:
                result["kev"].update({"status": "ok", "model_metadata": match})
            else:
                result["kev"].update({"status": "model_not_listed", "models": models})
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            result["kev"].update({"status": "error", "error": type(exc).__name__})
        if self.settings.luna_api_key:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.luna_timeout_seconds), trust_env=False) as client:
                    response = await client.get(
                        f"{self.settings.luna_base_url}/models",
                        headers={"Authorization": f"Bearer {self.settings.luna_api_key}"},
                    )
                if response.status_code == 401:
                    result["luna"]["status"] = "unauthorized"
                elif response.status_code >= 400:
                    result["luna"]["status"] = f"http_{response.status_code}"
                else:
                    data = response.json()
                    models = data.get("data", data.get("models", []))
                    names = [entry.get("id", entry.get("name")) for entry in models if isinstance(entry, dict)]
                    result["luna"].update({"status": "ok", "model_listed": self.settings.luna_model in names})
                result["luna"]["key_sent"] = True
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                result["luna"].update({"status": "error", "error": type(exc).__name__, "key_sent": True})
        return result

    @staticmethod
    def _menu_option_enabled(raw: dict[str, Any], option: str) -> bool:
        options = raw.get("options")
        if not isinstance(options, list) and raw.get("state_type") == "game_over":
            game_over = raw.get("game_over") if isinstance(raw.get("game_over"), dict) else {}
            options = game_over.get("options")
        if not isinstance(options, list):
            return False
        for item in options:
            if isinstance(item, str) and item.casefold() == option.casefold():
                return True
            if isinstance(item, dict) and str(item.get("name", "")).casefold() == option.casefold():
                return item.get("enabled") is True
        return False

    async def start_new_ironclad_run_step(self, profile_id: int = 2) -> dict[str, Any]:
        """Submit at most one advertised menu action to start a fresh Ironclad run."""
        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id not in {1, 2, 3}:
            raise DecisionPaused("profile_id must be one of the three game profiles.")
        if self._start_profile_id is None:
            self._start_profile_id = profile_id
        elif self._start_profile_id != profile_id:
            raise DecisionPaused("The start-run flow is already bound to a different profile; stop and inspect the current state.")

        raw = await self._read_passive_bridge_state()
        game_version = read_local_game_version(self.settings.game_dir)
        if self.settings.expected_game_version and game_version != self.settings.expected_game_version:
            raise DecisionPaused(
                f"Game version is {game_version or 'unknown'}, expected {self.settings.expected_game_version}; refusing to start a test run."
            )
        if raw.get("bridge_schema_version") != PASSIVE_BRIDGE_SCHEMA:
            raise DecisionPaused("Passive game bridge schema 2 is required before starting a test run.")

        state_type = str(raw.get("state_type", "unknown"))
        if state_type == "game_over":
            terminal = normalize_state(raw, game_version)
            if not terminal.complete:
                raise DecisionPaused("The game-over screen is incomplete; no menu action was submitted.")
            self.events.observe_state(raw, game_version, actions_enabled=self.settings.game_actions_enabled, character=None)
            menu_screen = "game_over"
            option = "main_menu"
        elif state_type != "menu":
            snapshot = normalize_state(raw, game_version)
            if snapshot.complete and snapshot.character and snapshot.character.casefold() == "ironclad":
                self.events.observe_state(raw, game_version, actions_enabled=self.settings.game_actions_enabled, character=snapshot.character)
                return {
                    "status": "run_started",
                    "profile_id": profile_id,
                    "state_type": snapshot.state_type,
                    "character": snapshot.character,
                    "state_fingerprint": snapshot.state_fingerprint,
                    "game_action_submitted": False,
                    "note": "An Ironclad run is already active; no menu action was sent.",
                }
            raise DecisionPaused("The game is not at a supported new-run menu and no complete Ironclad run is active.")

        else:
            menu_screen = raw.get("menu_screen")
            if not isinstance(menu_screen, str):
                raise DecisionPaused("Menu state is missing menu_screen; no menu action was sent.")

            if menu_screen == "main":
                if self._menu_option_enabled(raw, "continue"):
                    raise DecisionPaused("The active profile advertises a resumable run; refusing to start over it.")
                option = "singleplayer"
            elif menu_screen == "singleplayer":
                option = "standard"
            elif menu_screen == "profile_select":
                current_profile = raw.get("current_profile_id")
                if current_profile != profile_id:
                    raise DecisionPaused(
                        f"The game is currently on profile {current_profile!r}, not requested profile {profile_id}; no profile switch was made."
                    )
                option = f"profile_{profile_id}"
            elif menu_screen == "character_select":
                option = "embark" if self._start_ironclad_selected else "IRONCLAD"
            else:
                raise DecisionPaused(f"Starting a test run is unsupported on menu screen {menu_screen!r}.")

        event_state_type = "game_over" if state_type == "game_over" else "menu"
        before_fingerprint = fingerprint(raw)
        checkpoint = self._last_menu_action_checkpoint
        if checkpoint and checkpoint.get("state_fingerprint") == before_fingerprint:
            raise DecisionPaused(
                f"The previous menu action {checkpoint.get('option')} did not produce a verifiable state change; it was not retried."
            )
        self._last_menu_action_checkpoint = None

        if not self._menu_option_enabled(raw, option):
            raise DecisionPaused(f"The menu does not advertise enabled option {option!r}; no action was sent.")

        action_id = f"menu:{menu_screen}:{option.casefold()}"
        if not self.settings.game_actions_enabled:
            return {
                "status": "dry_run",
                "profile_id": profile_id,
                "menu_screen": menu_screen,
                "action": {"action": "menu_select", "option": option},
                "game_action_submitted": False,
                "state_fingerprint": before_fingerprint,
            }

        fresh = await self._read_passive_bridge_state()
        if fingerprint(fresh) != before_fingerprint:
            self.events.record(
                "proposal_discarded", provider="deterministic", state_type=event_state_type,
                game_version=game_version, status="stale_state",
                action_id=action_id,
                reason="Menu state changed before the guarded new-run action; no action POST was sent.",
                state_context={"profile_id": profile_id, "menu_screen": menu_screen, "options": raw.get("options")},
                state_fingerprint=before_fingerprint,
                state_after={"state_type": fresh.get("state_type"), "menu_screen": fresh.get("menu_screen"), "state_fingerprint": fingerprint(fresh)},
            )
            raise DecisionPaused("The menu state changed before submission; refresh and inspect it before continuing.")

        await self._pace()
        action_started = time.monotonic()
        payload = {"action": "menu_select", "option": option}
        try:
            result = await self.game.act(payload)
        except GameApiError as exc:
            observed: dict[str, Any] = {"status": "unavailable"}
            try:
                after = await self._read_passive_bridge_state()
                observed = {
                    "status": "read_ok",
                    "state_type": after.get("state_type"),
                    "menu_screen": after.get("menu_screen"),
                    "fingerprint_changed": fingerprint(after) != before_fingerprint,
                }
            except GameApiError:
                pass
            self._last_menu_action_checkpoint = {"state_fingerprint": before_fingerprint, "option": option}
            self.events.record(
                "action_outcome_unknown", provider="deterministic", state_type=event_state_type,
                game_version=game_version, status="outcome_unknown", action_id=action_id,
                reason="Menu action POST outcome is unknown; the menu action was not retried.",
                state_context={"profile_id": profile_id, "menu_screen": menu_screen, "option": option},
                state_fingerprint=before_fingerprint, duration_ms=(time.monotonic() - action_started) * 1000,
            )
            return {"status": "outcome_unknown", "profile_id": profile_id, "error": str(exc),
                    "reconciliation": observed, "retry": "do_not_retry; inspect the new state first",
                    "game_action_submitted": "unknown"}

        if result.get("status") != "ok":
            self.events.record(
                "action_rejected", provider="deterministic", state_type=event_state_type,
                game_version=game_version, status="rejected", action_id=action_id,
                reason="The game rejected the advertised new-run menu action.",
                state_context={"profile_id": profile_id, "menu_screen": menu_screen, "option": option},
                state_fingerprint=before_fingerprint, action_result=result,
                duration_ms=(time.monotonic() - action_started) * 1000,
            )
            return {"status": "action_rejected", "profile_id": profile_id, "response": result,
                    "game_action_submitted": False}

        deadline = time.monotonic() + 3.0
        after = raw
        after_fingerprint = before_fingerprint
        while time.monotonic() < deadline:
            after = await self._read_passive_bridge_state()
            after_fingerprint = fingerprint(after)
            if after_fingerprint != before_fingerprint:
                break
            await asyncio.sleep(0.12)

        self.events.record(
            "menu_action_submitted", provider="deterministic", state_type=event_state_type,
            game_version=game_version, confidence=1.0,
            status="accepted" if after_fingerprint != before_fingerprint else "no_progress",
            action_id=action_id, description=f"Start Ironclad run on profile {profile_id}: {menu_screen} → {option}.",
            reason="Submitted one visible, enabled menu option through the game MCP bridge; profile selection was restricted to the requested active profile.",
            state_context={"profile_id": profile_id, "menu_screen": menu_screen,
                           "options": raw.get("options"), "action": payload},
            action_result=result, state_fingerprint=before_fingerprint,
            state_after={"state_type": after.get("state_type"), "menu_screen": after.get("menu_screen"),
                         "state_fingerprint": after_fingerprint},
            duration_ms=(time.monotonic() - action_started) * 1000,
        )

        if after_fingerprint == before_fingerprint and option != "IRONCLAD":
            self._last_menu_action_checkpoint = {"state_fingerprint": before_fingerprint, "option": option}
            return {"status": "no_progress", "profile_id": profile_id, "action_id": action_id,
                    "state_fingerprint": after_fingerprint, "game_action_submitted": True,
                    "note": "The menu did not change after the action; it will not be repeated automatically."}

        if option == "IRONCLAD":
            self._start_ironclad_selected = True

        after_type = after.get("state_type")
        if after_type != "menu":
            started = normalize_state(after, game_version)
            if started.complete and started.character and started.character.casefold() == "ironclad":
                self.events.observe_state(after, game_version, actions_enabled=True, character=started.character)
                return {"status": "run_started", "profile_id": profile_id, "character": started.character,
                        "state_type": started.state_type, "state_fingerprint": started.state_fingerprint,
                        "game_action_submitted": True}
            return {"status": "paused", "profile_id": profile_id,
                    "reason": "The menu left character selection, but the resulting state is not a complete Ironclad run.",
                    "game_action_submitted": True}

        if after_fingerprint == before_fingerprint:
            return {"status": "menu_step_complete", "profile_id": profile_id, "action_id": action_id,
                    "state_fingerprint": after_fingerprint, "game_action_submitted": True,
                    "note": "Ironclad selection was accepted; the bridge snapshot did not expose a changed menu fingerprint."}
        return {"status": "menu_step_complete", "profile_id": profile_id, "action_id": action_id,
                "menu_screen": after.get("menu_screen"), "state_fingerprint": after_fingerprint,
                "game_action_submitted": True}

    async def plan_map_route(self, *, _snapshot: GameSnapshot | None = None) -> dict[str, Any]:
        snapshot = _snapshot or await self.snapshot()
        self._require_snapshot(snapshot)
        self.events.observe_state(snapshot.raw, snapshot.game_version, actions_enabled=self.settings.game_actions_enabled, character=snapshot.character)
        if snapshot.state_type != "map":
            raise DecisionPaused(f"Route planning requires a map screen; current screen is {snapshot.state_type}.")
        map_data = snapshot.raw.get("map", {})
        if not map_data.get("next_options"):
            raise DecisionPaused("Map has no currently reachable next node.")
        route_context = {
            "game_version": snapshot.game_version,
            "character": snapshot.character,
            "run": snapshot.raw.get("run"),
            "player": self._strategic_player_context(snapshot.raw, snapshot.character),
            "map": map_data,
            "rule": (
                "Return only the nodes AFTER map.current_position; do not include the current node. "
                "The first route point must exactly match one map.next_options item. "
                "Each later point must be a child edge of the immediately preceding point. "
                "Use coordinates exactly as written: col is column, row is floor. "
                "Include the boss as the final point and use only map.nodes/map.boss coordinates."
            ),
        }
        provider = self.mock if self.settings.provider_mode == "mock" else self.luna
        if self.settings.provider_mode not in {"mock", "live"}:
            raise DecisionPaused(f"Unknown agent.provider_mode {self.settings.provider_mode!r}; use mock or live.")
        if provider.name == "gpt-6-luna":
            route_context["luna_context"] = self.events.luna_context()
        decision_started = time.monotonic()
        try:
            answer = await provider.plan_route(state=route_context, min_confidence=self.settings.luna_min_confidence)
        except (ProviderError, ProviderResponseError, ProviderConfigurationError) as exc:
            self.events.record(
                "decision_paused", provider=provider.name, state_type=snapshot.state_type,
                game_version=snapshot.game_version,
                confidence=exc.confidence if isinstance(exc, LowConfidenceError) else None,
                status="low_confidence" if isinstance(exc, LowConfidenceError) else "provider_error",
                route=exc.route if isinstance(exc, LowConfidenceError) else None,
                reason=str(exc),
                state_context=route_context,
                duration_ms=(time.monotonic() - decision_started) * 1000,
            )
            raise DecisionPaused(str(exc)) from exc
        try:
            route = self._validate_route(map_data, answer.get("route"))
        except DecisionPaused as exc:
            self.events.record(
                "decision_paused", provider=provider.name, state_type=snapshot.state_type,
                game_version=snapshot.game_version, status="invalid_route", reason=str(exc),
                route=self._safe_route_candidate(answer.get("route")), state_context=route_context,
                confidence=answer.get("confidence"),
                duration_ms=(time.monotonic() - decision_started) * 1000,
            )
            raise
        if not route:
            raise DecisionPaused("Route planner returned no valid path to a boss.")
        if provider.name == "gpt-6-luna":
            luna_context = self.events.update_luna_context(
                answer.get("luna_context_update", {}), source=provider.name, state_type="map",
                floor=(snapshot.raw.get("run") or {}).get("floor"), action_id="route-plan",
            )
            if answer.get("luna_context_update"):
                self.events.record(
                    "luna_context_updated", provider=provider.name, state_type="map",
                    game_version=snapshot.game_version, status="route_context_refreshed",
                    action_id="route-plan", reason="Applied Luna's bounded strategy update after the route passed live graph validation.",
                    state_context={"luna_context": luna_context}, state_fingerprint=snapshot.state_fingerprint,
                )
        route_id = uuid4().hex[:12]
        graph_fingerprint = self._map_graph_fingerprint(map_data, (snapshot.raw.get("run") or {}).get("act"))
        self._routes[route_id] = {
            "route_id": route_id,
            "route": route,
            "cursor": 0,
            "created_at": time.monotonic(),
            "provider": provider.name,
            "confidence": float(answer["confidence"]),
            "reason": str(answer.get("reason", "")),
            "source_fingerprint": snapshot.state_fingerprint,
            "map_graph_fingerprint": graph_fingerprint,
        }
        self.events.record(
            "route_planned", provider=provider.name, state_type=snapshot.state_type,
            game_version=snapshot.game_version, confidence=float(answer["confidence"]),
            status="validated", route=route, reason=str(answer.get("reason", "")),
            state_context=route_context, state_fingerprint=snapshot.state_fingerprint,
            route_id=route_id, map_graph_fingerprint=graph_fingerprint,
            duration_ms=(time.monotonic() - decision_started) * 1000,
        )
        return {
            "status": "route_planned",
            "route_id": route_id,
            "game_version": snapshot.game_version,
            "provider": provider.name,
            "confidence": answer["confidence"],
            "reason": answer.get("reason", ""),
            "route": route,
            "note": "Planning did not move the character. Call recommend_next_action with this route_id; it validates the single reachable next node and selects it deterministically without another Luna or KEV call.",
        }

    async def _choose_with_verified_retry(
        self,
        provider: Any,
        *,
        state: dict[str, Any],
        actions: list[GameAction],
        min_confidence: float,
        original_snapshot: GameSnapshot,
    ) -> Decision:
        """Retry transient model transport failures only after proving state is unchanged."""
        max_attempts = 3 if provider.name == "kev" else 2 if provider.name == "gpt-6-luna" else 1
        provider_label = "KEV" if provider.name == "kev" else "GPT-6 Luna" if provider.name == "gpt-6-luna" else provider.name
        attempt = 1
        while True:
            try:
                return await provider.choose(state=state, actions=actions, min_confidence=min_confidence)
            except RetryableProviderError as exc:
                if provider.name == "kev" and "timed out" in str(exc).casefold():
                    # A timed-out Kev forward pass may still hold the server's
                    # single model lock. Replaying it would queue duplicate work.
                    raise
                if attempt >= max_attempts:
                    raise
                try:
                    fresh = await self.snapshot()
                except GameApiError as read_error:
                    self.events.record(
                        "provider_retry_aborted", provider=provider.name,
                        state_type=original_snapshot.state_type, game_version=original_snapshot.game_version,
                        status="state_unverified", reason=f"{provider_label} transport failed and fresh state read failed: {read_error}",
                        state_fingerprint=original_snapshot.state_fingerprint,
                        state_context={"attempt": attempt, "game_action_submitted": False},
                    )
                    raise DecisionPaused(f"Could not verify the game state after a {provider_label} transport failure; no game action was submitted.") from exc
                if (
                    not fresh.complete
                    or fresh.state_fingerprint != original_snapshot.state_fingerprint
                    or fresh.game_version != original_snapshot.game_version
                    or fresh.character != original_snapshot.character
                ):
                    self.events.record(
                        "provider_retry_aborted", provider=provider.name,
                        state_type=fresh.state_type, game_version=fresh.game_version,
                        status="state_changed", reason=f"{provider_label} transport failed and the complete live state changed; no inference retry or game action was sent.",
                        state_fingerprint=fresh.state_fingerprint,
                        state_context={"attempt": attempt, "game_action_submitted": False},
                    )
                    raise DecisionPaused(f"Game state changed after {provider_label} timed out; no game action was submitted; a fresh decision is required.") from exc
                self.events.record(
                    "provider_retry", provider=provider.name,
                    state_type=original_snapshot.state_type, game_version=original_snapshot.game_version,
                    status="retrying_same_state", reason=str(exc),
                    state_fingerprint=original_snapshot.state_fingerprint,
                    state_context={"attempt": attempt + 1, "max_attempts": max_attempts, "game_action_submitted": False},
                )
                await asyncio.sleep(0.25 * attempt)
                attempt += 1

    async def recommend_next_action(
        self, route_id: str | None = None, *, _snapshot: GameSnapshot | None = None
    ) -> dict[str, Any]:
        snapshot = _snapshot or await self.snapshot()
        self._require_snapshot(snapshot)
        self.events.observe_state(snapshot.raw, snapshot.game_version, actions_enabled=self.settings.game_actions_enabled, character=snapshot.character)
        if self.settings.provider_mode not in {"mock", "live"}:
            raise DecisionPaused(f"Unknown agent.provider_mode {self.settings.provider_mode!r}; use mock or live.")
        actions, catalog_errors = derive_actions(snapshot)
        if catalog_errors:
            raise DecisionPaused(" ".join(catalog_errors))
        if snapshot.state_type == "card_select":
            selection = snapshot.raw.get("card_select", {})
            if selection.get("preview_showing") is not True:
                selection_key = self._card_selection_fingerprint(snapshot.raw)
                if selection_key != self._card_selection_key:
                    self._card_selection_key = selection_key
                    self._selected_card_indices = self._restore_selected_cards(selection_key)
                actions = [
                    action for action in actions
                    if not (action.action == "select_card" and action.payload.get("index") in self._selected_card_indices)
                ]
        else:
            self._card_selection_key = None
            self._selected_card_indices.clear()
        prior_action = self._last_autoplay_submission or self._restore_last_autoplay_submission()
        if self._last_submission_made_no_progress(prior_action, snapshot.state_fingerprint):
            actions = [action for action in actions if action.action_id != prior_action.get("action_id")]
            if not actions:
                raise DecisionPaused(
                    f"{prior_action.get('action_id')} made no observable progress and no alternative legal action remains."
                )
        if snapshot.state_type == "map":
            if not route_id or route_id not in self._routes:
                raise DecisionPaused("Map movement needs a valid route_id from plan_map_route; planning and movement are separate steps.")
            route = self._routes[route_id]
            cursor = route["cursor"]
            if cursor >= len(route["route"]):
                raise DecisionPaused("The planned route is complete; request a new plan only if the map state warrants it.")
            expected = route["route"][cursor]
            wanted_id = f"map-node:{expected['row']}:{expected['col']}"
            actions = [action for action in actions if action.action_id == wanted_id]
            if not actions:
                del self._routes[route_id]
                raise DecisionPaused("The next planned node is no longer a current legal map choice; the plan was discarded. Re-read state and replan with Luna.")
            if len(actions) != 1:
                del self._routes[route_id]
                raise DecisionPaused("The planned route did not resolve to exactly one legal next node; the plan was discarded without a provider call.")
        combat_policy: dict[str, Any] | None = None
        if snapshot.state_type in {"monster", "elite", "boss"}:
            actions, combat_policy = combat_candidate_policy(snapshot.raw, actions)
        if not actions:
            reason = f"No supported legal action is visible on screen {snapshot.state_type}."
            self.events.record(
                "decision_paused", provider="none", state_type=snapshot.state_type,
                game_version=snapshot.game_version, status="no_legal_actions", reason=reason,
                state_context={"game_version": snapshot.game_version, "state_type": snapshot.state_type,
                               "screen": snapshot.raw.get(snapshot.state_type), "legal_action_ids": []},
                state_fingerprint=snapshot.state_fingerprint,
            )
            raise DecisionPaused(reason)

        decision_started = time.monotonic()
        context = self._context_for_provider(snapshot, actions, route_id, combat_policy)
        planned_action = self._resolve_planned_combat_step(
            snapshot.raw, actions,
            self._planned_combat_line.get("steps", [None])[0]
            if isinstance(self._planned_combat_line, dict) else None,
        ) if snapshot.state_type in {"monster", "elite", "boss"} else None
        if isinstance(self._planned_combat_line, dict) and planned_action is None:
            discarded = self._planned_combat_line
            self._planned_combat_line = None
            self.events.record(
                "combat_line_discarded", provider="kev", state_type=snapshot.state_type,
                game_version=snapshot.game_version, status="next_step_not_currently_legal",
                reason="The re-read MCP state no longer exposes the next action from the selected line; discarded its continuation and will decide from current legal actions.",
                state_context={
                    "line_choice_id": discarded.get("line_choice_id"),
                    "remaining_step_count": len(discarded.get("steps", [])),
                    "next_step": (discarded.get("steps") or [None])[0],
                    "legal_action_ids": [action.action_id for action in actions],
                },
                state_fingerprint=snapshot.state_fingerprint,
            )
        elif isinstance(self._planned_combat_line, dict):
            active_line = self._planned_combat_line
            context["selected_combat_line"] = {
                "line_choice_id": active_line.get("line_choice_id"),
                "remaining_steps": active_line.get("steps"),
                "source_confidence": active_line.get("confidence"),
                "continuation": True,
            }
        # Map movement has already been reduced to the sole reachable node on
        # Luna's validated route, so it is deterministic and needs no provider.
        deterministic = self._deterministic_action(snapshot, actions) if planned_action is None else None
        if planned_action is not None and isinstance(self._planned_combat_line, dict):
            active_line = self._planned_combat_line
            decision = Decision(
                action_id=planned_action.action_id,
                provider="kev",
                confidence=float(active_line.get("confidence", 1.0)),
                reason=(
                    f"Continue KEV-selected whole-turn line {active_line.get('line_choice_id')}; "
                    "this step was rebound to the freshly read legal action set."
                ),
                probabilities={planned_action.action_id: 1.0},
                plan_steps=tuple(active_line.get("steps", [])),
                line_choice_id=active_line.get("line_choice_id"),
            )
        elif deterministic is not None:
            if snapshot.state_type == "map":
                reason = "The validated route's sole reachable next node was selected deterministically without calling a provider."
            elif len(actions) == 1:
                reason = "Exactly one legal action remained after live-state validation; selected it directly without calling KEV or Luna."
            else:
                reason = "No strategy model was needed for this forced or routine screen action."
            decision = Decision(deterministic.action_id, "deterministic", 1.0, reason)
        else:
            provider, min_confidence = self._decision_provider(snapshot)
            if provider.name == "gpt-6-luna":
                context["luna_context"] = self.events.luna_context()
                if snapshot.state_type == "card_reward" and not self._has_complete_live_deck(context):
                    reason = "Luna card-reward selection needs the complete current player.deck from the game Mod; no provider was called and no action was proposed. Update/restart the local STS2 MCP Mod first."
                    self.events.record(
                        "decision_paused", provider=provider.name, state_type=snapshot.state_type,
                        game_version=snapshot.game_version, status="missing_live_deck",
                        reason=reason, state_context=context, state_fingerprint=snapshot.state_fingerprint,
                    )
                    raise DecisionPaused(reason)
            try:
                decision = await self._choose_with_verified_retry(
                    provider, state=context, actions=actions, min_confidence=min_confidence,
                    original_snapshot=snapshot,
                )
            except (ProviderError, ProviderResponseError, ProviderConfigurationError) as exc:
                self.events.record(
                    "decision_paused", provider=provider.name, state_type=snapshot.state_type,
                    game_version=snapshot.game_version,
                    confidence=exc.confidence if isinstance(exc, LowConfidenceError) else None,
                    status="low_confidence" if isinstance(exc, LowConfidenceError) else "provider_error",
                    action_id=exc.action_id if isinstance(exc, LowConfidenceError) else None,
                    candidates=[
                        {**action.public(), "probability": exc.probabilities[action.action_id]}
                        if isinstance(exc, LowConfidenceError) and action.action_id in exc.probabilities
                        else action.public()
                        for action in actions
                    ],
                    probabilities=exc.probabilities if isinstance(exc, LowConfidenceError) else None,
                    reason=str(exc),
                    state_context=context, state_fingerprint=snapshot.state_fingerprint,
                    duration_ms=(time.monotonic() - decision_started) * 1000,
                )
                raise DecisionPaused(str(exc)) from exc
        selected = next((action for action in actions if action.action_id == decision.action_id), None)
        if selected is not None and snapshot.state_type in COMBAT_SCREENS:
            guarded = emergency_defense_override(
                snapshot.raw, actions, selected, combat_policy=combat_policy
            )
            if guarded is not None:
                replacement, pressure = guarded
                previous_id = decision.action_id
                guard_reason = pressure.get("guard_reason", "material_incoming_damage")
                prevented = pressure.get("preventable_hp_loss_estimate", 0)
                replacement_hp_loss = pressure.get(
                    "replacement_projected_hp_loss_if_turn_ended",
                    pressure.get("projected_hp_loss_after_selected_block"),
                )
                replacement_total_hp_loss = pressure.get(
                    "replacement_projected_total_hp_loss", replacement_hp_loss
                )
                replacement_label = (
                    "verified surviving current-turn line"
                    if pressure.get("guard_reason") == "lethal_turn_supported_line_preserves_survival" else
                    "lowest-loss supported line when all modeled lines are lethal"
                    if pressure.get("guard_reason") == "all_modeled_lines_lethal_minimize_uncapped_hp_loss" else
                    "verified attacker kill"
                    if pressure.get("replacement_kills_attacking_enemy")
                    else "Block action"
                )
                self.events.record(
                    "combat_survival_guard", provider=decision.provider, state_type=snapshot.state_type,
                    game_version=snapshot.game_version, confidence=decision.confidence,
                    status="replaced_by_combat_outcome_guard", action_id=replacement.action_id,
                    description=replacement.description,
                    candidates=[
                        {**action.public(), "probability": decision.probabilities[action.action_id]}
                        if action.action_id in decision.probabilities else action.public()
                        for action in actions
                    ],
                    probabilities=decision.probabilities,
                    reason=(
                        f"{guard_reason}: replaced {previous_id} before its energy was spent; "
                        f"the {replacement_label} is estimated to leave {replacement_total_hp_loss} total HP loss "
                        f"and prevent {prevented} HP loss this turn. "
                        f"Rejected action estimate: {pressure.get('rejected_action_attack_hp_damage_estimate')} HP damage, "
                        f"{pressure.get('rejected_action_enemy_block_removed_estimate')} enemy Block removed."
                    ),
                    state_context={**context, "combat_pressure": pressure, "rejected_action_id": previous_id},
                    state_fingerprint=snapshot.state_fingerprint,
                )
                guarded_line = next((
                    item for item in (combat_policy or {}).get("turn_line_candidates", [])
                    if isinstance(item, dict)
                    and item.get("step_action_ids") == pressure.get("replacement_line_action_ids")
                ), None)
                decision = replace(
                    decision, action_id=replacement.action_id,
                    reason=(
                        f"Combat outcome guard ({guard_reason}) overrode {previous_id}: current visible HP loss is "
                        f"{pressure.get('projected_hp_loss_if_turn_ended_now')}; the replacement is estimated to leave "
                        f"{replacement_total_hp_loss} total HP loss and prevent {prevented}. Rejected action estimates: "
                        f"{pressure.get('rejected_action_attack_hp_damage_estimate')} HP damage and "
                        f"{pressure.get('rejected_action_enemy_block_removed_estimate')} enemy Block removed. {decision.reason}"
                    )[:500],
                    plan_steps=tuple(guarded_line.get("steps", [])) if isinstance(guarded_line, dict) else (),
                    line_choice_id=guarded_line.get("line_id") if isinstance(guarded_line, dict) else None,
                    line_probabilities={},
                )
                self._planned_combat_line = None
        if decision.action_id not in {action.action_id for action in actions}:
            raise DecisionPaused("Provider choice is not in the current legal action set.")
        if decision.line_choice_id:
            context["selected_combat_line"] = {
                "line_choice_id": decision.line_choice_id,
                "steps": list(decision.plan_steps),
                "line_probabilities": decision.line_probabilities,
                "continuation": False,
            }
        proposal_id = uuid4().hex[:12]
        decision = replace(decision, state_fingerprint=snapshot.state_fingerprint)
        chosen = next(action for action in actions if action.action_id == decision.action_id)
        self._pending[proposal_id] = {
            "action": chosen,
            "decision": decision,
            "state_fingerprint": snapshot.state_fingerprint,
            "route_id": route_id,
            "created_at": time.monotonic(),
            "state_type": snapshot.state_type,
            "game_version": snapshot.game_version,
            "state_context": context,
            "proposal_id": proposal_id,
        }
        self.events.record(
            "decision_proposed", provider=decision.provider, state_type=snapshot.state_type,
            game_version=snapshot.game_version, confidence=decision.confidence, status="validated",
            action_id=chosen.action_id, description=chosen.description,
            candidates=[
                {**action.public(), "probability": decision.probabilities[action.action_id]}
                if action.action_id in decision.probabilities else action.public()
                for action in actions
            ],
            probabilities=decision.probabilities, reason=decision.reason,
            state_context=context, proposal_id=proposal_id,
            route_id=route_id,
            state_fingerprint=snapshot.state_fingerprint,
            duration_ms=(time.monotonic() - decision_started) * 1000,
        )
        return {
            "status": "proposal_ready",
            "proposal_id": proposal_id,
            "state_type": snapshot.state_type,
            "game_version": snapshot.game_version,
            "provider": decision.provider,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "probabilities": decision.probabilities,
            "action": chosen.public(),
            "state_fingerprint": snapshot.state_fingerprint,
            "dry_run": not self.settings.game_actions_enabled,
            "line_choice_id": decision.line_choice_id,
            "planned_line_steps": list(decision.plan_steps),
        }

    @staticmethod
    def _resolve_planned_combat_step(
        state: dict[str, Any], actions: list[GameAction], selector: Any,
    ) -> GameAction | None:
        """Rebind a previously selected line step to an exact current legal action."""
        if not isinstance(selector, dict):
            return None
        kind = selector.get("kind")
        matches: list[GameAction] = []
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        hand = player.get("hand") if isinstance(player.get("hand"), list) else []
        potions = player.get("potions") if isinstance(player.get("potions"), list) else []
        for action in actions:
            if kind == "end_turn":
                if action.action == "end_turn":
                    matches.append(action)
                continue
            if kind == "play_card" and action.action == "play_card":
                index = action.payload.get("card_index")
                card = None
                if isinstance(index, int):
                    card = next((
                        item for fallback, item in enumerate(hand)
                        if isinstance(item, dict) and item.get("index", fallback) == index
                    ), None)
                    if card is None and 0 <= index < len(hand) and isinstance(hand[index], dict):
                        card = hand[index]
                if not isinstance(card, dict):
                    continue
                if card.get("id") != selector.get("card_id") or card.get("name") != selector.get("card_name"):
                    continue
                expected_type = selector.get("card_type")
                if expected_type is not None and card.get("type") != expected_type:
                    continue
                expected_upgraded = selector.get("is_upgraded")
                actual_upgraded = card.get("is_upgraded", card.get("upgraded"))
                if expected_upgraded is not None and actual_upgraded is not None and actual_upgraded != expected_upgraded:
                    continue
                if action.payload.get("target") != selector.get("target_id"):
                    continue
                matches.append(action)
            elif kind == "use_potion" and action.action == "use_potion":
                slot = action.payload.get("slot")
                potion = next((
                    item for item in potions
                    if isinstance(item, dict) and item.get("slot") == slot
                ), None)
                if not isinstance(potion, dict):
                    continue
                if potion.get("id") != selector.get("potion_id") or potion.get("name") != selector.get("potion_name"):
                    continue
                if action.payload.get("target") != selector.get("target_id"):
                    continue
                matches.append(action)
        if not matches:
            return None
        return min(matches, key=lambda item: (
            int(item.payload.get("card_index", item.payload.get("slot", 0)))
            if isinstance(item.payload.get("card_index", item.payload.get("slot", 0)), int) else 0,
            item.action_id,
        ))

    async def execute_proposal(self, proposal_id: str) -> dict[str, Any]:
        pending = self._pending.pop(proposal_id, None)
        if pending is None:
            raise DecisionPaused("Proposal does not exist or was already used; request a fresh decision.")
        if time.monotonic() - pending["created_at"] > self.settings.max_state_age_seconds:
            self._planned_combat_line = None
            raise DecisionPaused("Proposal expired before execution; request a fresh state and decision.")
        if not self.settings.game_actions_enabled:
            self.events.record(
                "action_preview", provider=pending["decision"].provider,
                state_type=pending.get("state_type"), game_version=pending.get("game_version"),
                confidence=pending["decision"].confidence, status="dry_run",
                action_id=pending["action"].action_id,
                description=pending["action"].description,
                state_context=pending.get("state_context"),
                proposal_id=proposal_id, state_fingerprint=pending.get("state_fingerprint"),
            )
            return {
                "status": "dry_run",
                "executed": False,
                "action": pending["action"].public(),
                "provider": pending["decision"].provider,
                "confidence": pending["decision"].confidence,
                "note": "Enable agent.game_actions_enabled only when ready for a real in-game action. This project is currently preserving Steam runs.",
            }
        current = await self.snapshot()
        self._require_snapshot(current)
        if current.state_fingerprint != pending["state_fingerprint"]:
            self._planned_combat_line = None
            self.events.record(
                "proposal_discarded", provider=pending["decision"].provider,
                state_type=pending.get("state_type"), game_version=pending.get("game_version"),
                confidence=pending["decision"].confidence, status="stale_state",
                action_id=pending["action"].action_id, description=pending["action"].description,
                reason="Proposal was discarded because the game state changed before submission; no action POST was sent.",
                state_context=pending.get("state_context"), proposal_id=proposal_id,
                state_fingerprint=pending.get("state_fingerprint"),
                state_after={"state_type": current.state_type, "state_fingerprint": current.state_fingerprint,
                             "complete": current.complete, "errors": list(current.errors)},
            )
            raise DecisionPaused("Game state changed after the proposal; it was discarded without submitting an action.")
        actions, errors = derive_actions(current)
        if errors:
            raise DecisionPaused(" ".join(errors))
        legal = next((a for a in actions if a.action_id == pending["action"].action_id), None)
        if legal is None or legal.payload != pending["action"].payload:
            self._planned_combat_line = None
            raise DecisionPaused("Proposed action is no longer legal or its parameters changed; nothing was submitted.")
        await self._pace()
        action_started = time.monotonic()
        try:
            result = await self.game.act(legal.payload)
        except GameApiError as exc:
            self._planned_combat_line = None
            # A timeout can mean the game accepted the input but the reply was lost. Read once to
            # help the caller reconcile; never replay the POST automatically.
            observed: dict[str, Any] = {"status": "unavailable"}
            try:
                fresh = await self.snapshot()
                observed = {
                    "status": "read_ok",
                    "state_type": fresh.state_type,
                    "fingerprint_changed": fresh.state_fingerprint != pending["state_fingerprint"],
                }
            except GameApiError:
                pass
            self.events.record(
                "action_outcome_unknown", provider=pending["decision"].provider,
                state_type=pending.get("state_type"), game_version=pending.get("game_version"),
                confidence=pending["decision"].confidence, status="outcome_unknown",
                action_id=legal.action_id, description=legal.description,
                reason="Game action POST outcome is unknown; the action was not retried.",
                state_context=pending.get("state_context"), proposal_id=proposal_id,
                state_fingerprint=pending.get("state_fingerprint"),
                duration_ms=(time.monotonic() - action_started) * 1000,
            )
            return {"status": "outcome_unknown", "executed": "unknown", "error": str(exc),
                    "reconciliation": observed, "retry": "do_not_retry; inspect the new state first"}
        if result.get("status") != "ok":
            self._planned_combat_line = None
            self.events.record(
                "action_rejected", provider=pending["decision"].provider,
                state_type=pending.get("state_type"), game_version=pending.get("game_version"),
                confidence=pending["decision"].confidence, status="rejected",
                action_id=legal.action_id, description=legal.description,
                state_context=pending.get("state_context"), proposal_id=proposal_id,
                state_fingerprint=pending.get("state_fingerprint"), action_result=result,
                duration_ms=(time.monotonic() - action_started) * 1000,
            )
            return {"status": "action_rejected", "executed": False, "response": result,
                    "note": "Refresh the game state before making another decision."}
        self._commit_luna_context(pending, legal)
        if legal.action == "select_card":
            self._track_card_selection(pending)
        route_id = pending.get("route_id")
        if legal.action == "choose_map_node" and route_id in self._routes:
            self._routes[route_id]["cursor"] += 1
        try:
            if legal.action == "choose_map_node":
                # Map selection animations can still report the old complete map state
                # briefly. Wait for the selected room screen before planning the next node.
                fresh = await self._wait_for_settled_state(
                    timeout_seconds=3.0,
                    wait_for_state_type_change_from=pending.get("state_type"),
                )
            else:
                fresh = await self._wait_for_settled_state()
            self._autoplay_next_snapshot = fresh
            updated = {
                "state_type": fresh.state_type,
                "state_fingerprint": fresh.state_fingerprint,
                "complete": fresh.complete,
                "errors": list(fresh.errors),
            }
        except GameApiError:
            self._autoplay_next_snapshot = None
            updated = {"status": "state_refresh_failed"}
            fresh = None
        decision: Decision = pending["decision"]
        if (
            decision.line_choice_id
            and decision.provider == "kev"
            and decision.plan_steps
            and updated.get("complete") is True
            and isinstance(fresh, GameSnapshot)
            and fresh.state_type in {"monster", "elite", "boss"}
            and len(decision.plan_steps) > 1
        ):
            self._planned_combat_line = {
                "line_choice_id": decision.line_choice_id,
                "steps": list(decision.plan_steps[1:]),
                "confidence": decision.confidence,
                "source_state_fingerprint": pending.get("state_fingerprint"),
            }
        else:
            self._planned_combat_line = None
        self.events.record(
            "action_submitted", provider=pending["decision"].provider,
            state_type=pending.get("state_type"), game_version=pending.get("game_version"),
            confidence=pending["decision"].confidence, status="submitted",
            action_id=legal.action_id, description=legal.description,
            probabilities=pending["decision"].probabilities,
            state_context=pending.get("state_context"), proposal_id=proposal_id,
            route_id=pending.get("route_id"),
            state_fingerprint=pending.get("state_fingerprint"), action_result=result,
            state_after=updated,
            reason=(
                f"Next screen: {updated.get('state_type', 'unknown')}."
                if updated.get("complete") is True
                else f"Action was submitted once; state has not settled yet: {updated.get('errors', updated.get('status', 'unknown'))}."
            ),
            duration_ms=(time.monotonic() - action_started) * 1000,
        )
        if updated.get("complete") is True and updated.get("state_fingerprint"):
            self._last_autoplay_submission = {
                "action_id": legal.action_id,
                "submitted_state_fingerprint": pending["state_fingerprint"],
                "state_fingerprint": updated["state_fingerprint"],
                "state_type": updated.get("state_type"),
                "proposal_id": proposal_id,
            }
        return {"status": "action_submitted", "executed": True, "provider": pending["decision"].provider,
                "confidence": pending["decision"].confidence, "action": legal.public(),
                "game_response": result, "state_after": updated}

    async def step_once(
        self, route_id: str | None = None, *, _snapshot: GameSnapshot | None = None
    ) -> dict[str, Any]:
        proposal = await self.recommend_next_action(route_id, _snapshot=_snapshot)
        if not self.settings.game_actions_enabled:
            proposal["status"] = "dry_run_proposal"
            proposal["executed"] = False
            return proposal
        return await self.execute_proposal(proposal["proposal_id"])

    async def autoplay(self, batch_size: int = 12, stop_after_act_one_boss: bool = False) -> dict[str, Any]:
        """Immediately chain guarded MCP decisions, stopping on any unresolved condition."""
        if not self.settings.game_actions_enabled:
            return {"status": "paused", "reason": "Live autoplay requires game_actions_enabled=true in the process-local config.", "steps": []}
        if self.settings.provider_mode != "live":
            return {"status": "paused", "reason": "Live autoplay requires provider_mode='live'.", "steps": []}
        try:
            count = int(batch_size)
        except (TypeError, ValueError):
            return {"status": "paused", "reason": "batch_size must be an integer between 1 and 20.", "steps": []}
        if not 1 <= count <= 20:
            return {"status": "paused", "reason": "batch_size must be between 1 and 20.", "steps": []}

        steps: list[dict[str, Any]] = []
        route_id = self._autoplay_route_id
        stale_rechecks = 0
        empty_action_rechecks = 0
        targetless_rechecks = 0
        while len(steps) < count:
            try:
                snapshot = self._autoplay_next_snapshot or await self.snapshot()
                self._autoplay_next_snapshot = None
                if (
                    not snapshot.complete
                    and snapshot.state_type in COMBAT_SCREENS
                    and snapshot.errors
                    and set(snapshot.errors).issubset(TRANSIENT_COMBAT_STATE_ERRORS)
                ):
                    # End turn and combat end are asynchronous: the bridge can
                    # briefly expose the enemy phase or a dismantling combat screen.
                    # Keep polling read-only until a complete player/screen state lands.
                    prior_combat_fingerprint = snapshot.state_fingerprint
                    snapshot = await self._wait_for_settled_state(timeout_seconds=45.0)
                    if snapshot.complete:
                        self.events.record(
                            "autoplay_combat_state_recovery", provider="none",
                            state_type=snapshot.state_type, game_version=snapshot.game_version,
                            status="complete_state_ready",
                            reason="A transient combat snapshot was incomplete; read-only MCP polling waited for the next complete player phase or screen.",
                            source_fingerprint=prior_combat_fingerprint,
                            state_fingerprint=snapshot.state_fingerprint,
                        )
                self.events.observe_state(snapshot.raw, snapshot.game_version, actions_enabled=True, character=snapshot.character)
                if snapshot.state_type in TERMINAL_SCREEN_TYPES:
                    return {"status": "run_ended", "reason": f"Run reached terminal screen {snapshot.state_type}.", "last_state_type": snapshot.state_type, "steps": steps}
                if not snapshot.complete:
                    return {"status": "run_ended" if snapshot.state_type in TERMINAL_SCREEN_TYPES else "paused",
                            "reason": " ".join(snapshot.errors), "last_state_type": snapshot.state_type, "steps": steps}
                self._require_snapshot(snapshot)
                last_action = self._last_autoplay_submission or self._restore_last_autoplay_submission()
                if self._last_submission_made_no_progress(last_action, snapshot.state_fingerprint):
                    selection = snapshot.raw.get("card_select", {}) if snapshot.state_type == "card_select" else {}
                    legal_now, legal_errors = derive_actions(snapshot)
                    can_confirm_enchant = (
                        snapshot.state_type == "card_select"
                        and str(last_action.get("action_id", "")).startswith("deck-card:")
                        and isinstance(selection, dict)
                        and selection.get("screen_type") == "NDeckEnchantSelectScreen"
                        and selection.get("can_confirm") is True
                        and not legal_errors
                        and any(action.action == "confirm_selection" for action in legal_now)
                    )
                    can_skip_full_potion_item = self._is_full_potion_item_noop(
                        snapshot, last_action, legal_now, legal_errors
                    )
                    if can_confirm_enchant:
                        self.events.record(
                            "action_no_progress_alternative", provider="none",
                            state_type=snapshot.state_type, game_version=snapshot.game_version,
                            status="confirm_available", action_id=last_action.get("action_id"),
                            reason="A card-selection click made no state change, but the enchant screen exposes its enabled confirm action; the repeated card click is filtered and confirm is tried once.",
                            state_fingerprint=snapshot.state_fingerprint,
                        )
                    elif can_skip_full_potion_item:
                        self.events.record(
                            "action_no_progress_alternative", provider="none",
                            state_type=snapshot.state_type, game_version=snapshot.game_version,
                            status="full_potion_inventory", action_id=last_action.get("action_id"),
                            reason="A potion claim or purchase left the complete state unchanged while every potion slot was occupied. That item is removed from the legal set and autoplay continues with a fresh decision; it is never replayed.",
                            state_fingerprint=snapshot.state_fingerprint,
                        )
                        # Keep the unchanged purchase from being restored as a new
                        # no-progress event on the next loop. The action catalog
                        # independently removes potion purchases while all slots
                        # are occupied, so this cannot resubmit the failed action.
                        self._last_autoplay_submission = {
                            "action_id": last_action.get("action_id"),
                            "submitted_state_fingerprint": None,
                            "state_fingerprint": snapshot.state_fingerprint,
                            "state_type": snapshot.state_type,
                        }
                    else:
                        reason = (
                            f"{last_action.get('action_id')} returned without changing the complete game state; "
                            "autoplay paused before asking a provider or sending another game action."
                        )
                        self.events.record(
                            "action_no_progress_guard", provider="none",
                            state_type=snapshot.state_type, game_version=snapshot.game_version,
                            status="unchanged_state", action_id=last_action.get("action_id"), reason=reason,
                            state_fingerprint=snapshot.state_fingerprint,
                            state_after={"state_type": snapshot.state_type, "state_fingerprint": snapshot.state_fingerprint,
                                         "complete": snapshot.complete},
                        )
                        return {"status": "paused", "reason": reason, "last_state_type": snapshot.state_type, "steps": steps}
                if snapshot.state_type == "map":
                    graph_fingerprint = self._map_graph_fingerprint(
                        snapshot.raw.get("map", {}), (snapshot.raw.get("run") or {}).get("act")
                    )
                    current_route = self._routes.get(route_id) if route_id else None
                    if current_route is None or current_route.get("map_graph_fingerprint") != graph_fingerprint:
                        route_id = self._restore_route(snapshot, graph_fingerprint)
                        current_route = self._routes.get(route_id) if route_id else None
                        if route_id:
                            self._autoplay_route_id = route_id
                    if (
                        current_route is None
                        or current_route.get("map_graph_fingerprint") != graph_fingerprint
                        or current_route.get("cursor", 0) >= len(current_route.get("route", []))
                    ):
                        # One complete route plan per map graph. Following its nodes never calls Luna.
                        plan = await self.plan_map_route(_snapshot=snapshot)
                        route_id = plan["route_id"]
                        self._autoplay_route_id = route_id
                proposal = await self.recommend_next_action(route_id, _snapshot=snapshot)
                receipt = await self.execute_proposal(proposal["proposal_id"])
            except DecisionPaused as exc:
                if str(exc) in TRANSIENT_COMBAT_STATE_ERRORS:
                    prior_combat_fingerprint = snapshot.state_fingerprint
                    try:
                        fresh = await self._wait_for_actionable_state(timeout_seconds=45.0)
                    except GameApiError as read_error:
                        return {
                            "status": "paused",
                            "reason": f"Combat phase was transient and a fresh MCP state could not be read: {read_error}",
                            "last_state_type": snapshot.state_type,
                            "steps": steps,
                        }
                    fresh_actions, fresh_errors = derive_actions(fresh) if fresh.complete else ([], list(fresh.errors))
                    if fresh.complete and fresh_actions and not fresh_errors:
                        self._autoplay_next_snapshot = fresh
                        self.events.record(
                            "autoplay_combat_state_recovery", provider="none",
                            state_type=fresh.state_type, game_version=fresh.game_version,
                            status="actionable_state_ready",
                            reason="Combat was between player and enemy phases; read-only MCP polling waited until the current screen exposed legal actions.",
                            source_fingerprint=prior_combat_fingerprint,
                            state_fingerprint=fresh.state_fingerprint,
                        )
                        continue
                    return {
                        "status": "paused",
                        "reason": "Combat did not expose a complete legal-action state before the 45-second read-only phase wait expired.",
                        "last_state_type": fresh.state_type,
                        "steps": steps,
                    }
                if "no living enemy is visible" in str(exc):
                    targetless_rechecks += 1
                    self._autoplay_next_snapshot = None
                    try:
                        fresh = await self.snapshot()
                    except GameApiError as read_error:
                        return {"status": "paused", "reason": f"Combat ended or changed during target validation, but the fresh state could not be read: {read_error}", "steps": steps}
                    if fresh.complete:
                        fresh_actions, fresh_errors = derive_actions(fresh)
                        if fresh_actions and not fresh_errors:
                            self._autoplay_next_snapshot = fresh
                            self.events.record(
                                "autoplay_combat_transition_recovery", provider="none",
                                state_type=fresh.state_type, game_version=fresh.game_version,
                                status="new_state_after_targetless_combat",
                                reason="The combat snapshot briefly had no living target; a read-only MCP refresh now exposes the next supported screen.",
                                state_fingerprint=fresh.state_fingerprint,
                            )
                            targetless_rechecks = 0
                            continue
                    if targetless_rechecks < 4:
                        await asyncio.sleep(0.15)
                        continue
                    return {"status": "paused", "reason": "Combat still exposes no living enemy and the state has not advanced after four fresh MCP reads.", "last_state_type": fresh.state_type, "steps": steps}
                # A proposal can become stale while a screen settles, or the live
                # state can change while a read-only KEV request is timing out.
                # In both cases no game action was sent; read again and decide from
                # the fresh complete state instead of pausing the whole run.
                if str(exc) in {
                    "Game state changed after the proposal; it was discarded without submitting an action.",
                    "Game state changed after KEV timed out; no game action was submitted; a fresh decision is required.",
                }:
                    stale_rechecks += 1
                    self._autoplay_next_snapshot = None
                    if stale_rechecks <= 3:
                        try:
                            fresh = await self.snapshot()
                        except GameApiError as read_error:
                            return {"status": "paused", "reason": f"Stale proposal was safely discarded, but refreshed state could not be read: {read_error}", "steps": steps}
                        if fresh.complete:
                            self._autoplay_next_snapshot = fresh
                            self.events.record(
                                "autoplay_provider_state_recovery" if "KEV timed out" in str(exc) else "autoplay_stale_recovery", provider="none",
                                state_type=fresh.state_type, game_version=fresh.game_version,
                                status="refreshed_without_action",
                                reason=(
                                    "KEV inference was discarded after the game state changed; no game action POST was sent, and autoplay resumed from a fresh complete state."
                                    if "KEV timed out" in str(exc) else
                                    "Discarded stale proposal without a game action POST; continuing from a fresh complete state."
                                ),
                                state_fingerprint=fresh.state_fingerprint,
                            )
                            continue
                    return {"status": "paused", "reason": f"Stale proposal was safely discarded without a game action POST; state remained unstable after {stale_rechecks} refresh attempts.", "last_state_type": locals().get("snapshot").state_type if "snapshot" in locals() else None, "steps": steps}
                if str(exc).startswith("No supported legal action is visible on screen "):
                    empty_action_rechecks += 1
                    try:
                        fresh = await self.snapshot()
                    except GameApiError as read_error:
                        return {"status": "paused", "reason": f"No legal action was visible; refreshed state could not be read: {read_error}", "steps": steps}
                    if not fresh.complete:
                        return {"status": "paused", "reason": "No legal action was visible and the refreshed game state is incomplete: " + " ".join(fresh.errors), "last_state_type": fresh.state_type, "steps": steps}
                    fresh_actions, fresh_errors = derive_actions(fresh)
                    if fresh_errors:
                        return {"status": "paused", "reason": "No legal action was visible and the refreshed action catalog is unsupported: " + " ".join(fresh_errors), "last_state_type": fresh.state_type, "steps": steps}
                    if fresh_actions:
                        self._autoplay_next_snapshot = fresh
                        self.events.record(
                            "autoplay_empty_action_recovery", provider="none",
                            state_type=fresh.state_type, game_version=fresh.game_version,
                            status="actions_visible_after_refresh",
                            reason="The first complete snapshot exposed no legal action; a fresh MCP read now exposes a validated legal action.",
                            state_fingerprint=fresh.state_fingerprint,
                        )
                        empty_action_rechecks = 0
                        continue
                    self._autoplay_next_snapshot = None
                    if empty_action_rechecks < 10:
                        # Some screens (notably RestSite after an accepted rest)
                        # settle their exit/proceed control a few MCP reads after
                        # the selection action. Keep polling passively; never
                        # synthesize or resubmit a game action during this wait.
                        await asyncio.sleep(0.2)
                        continue
                    return {"status": "paused", "reason": f"No supported legal action is visible on screen {fresh.state_type} after {empty_action_rechecks} fresh MCP reads.", "last_state_type": fresh.state_type, "steps": steps}
                return {"status": "paused", "reason": str(exc), "last_state_type": locals().get("snapshot").state_type if "snapshot" in locals() else None, "steps": steps}
            except (GameApiError, ProviderError, ProviderResponseError, ProviderConfigurationError) as exc:
                return {"status": "paused", "reason": str(exc), "steps": steps}

            steps.append({
                "status": receipt.get("status"), "state_type": receipt.get("state_type", snapshot.state_type),
                "provider": receipt.get("provider"), "action": receipt.get("action"),
                "confidence": receipt.get("confidence"), "state_after": receipt.get("state_after"),
            })
            if receipt.get("status") != "action_submitted" or receipt.get("executed") is not True:
                return {"status": "paused", "reason": receipt.get("note", receipt.get("error", receipt.get("status", "Action was not confirmed as submitted."))), "last_state_type": snapshot.state_type, "steps": steps}
            stale_rechecks = 0
            empty_action_rechecks = 0
            targetless_rechecks = 0
            after = receipt.get("state_after", {})
            before_act = (snapshot.raw.get("run") or {}).get("act")
            was_act_one_boss = snapshot.state_type == "boss" and before_act == 1
            if stop_after_act_one_boss and was_act_one_boss:
                boss_after = self._autoplay_next_snapshot
                if boss_after is None and after.get("complete") is True:
                    try:
                        boss_after = await self.snapshot()
                    except GameApiError:
                        boss_after = None
                if boss_after is not None and boss_after.complete and boss_after.state_type not in COMBAT_SCREENS:
                    boss_after_act = (boss_after.raw.get("run") or {}).get("act")
                    if boss_after_act in {1, 2} or boss_after.state_type in TERMINAL_SCREEN_TYPES:
                        self.events.record(
                            "act_one_boss_cleared", provider="none", state_type="boss",
                            game_version=snapshot.game_version, status="first_boss_defeated",
                            action_id=(receipt.get("action") or {}).get("action_id"),
                            reason="A complete post-action MCP state left the Act 1 boss combat; autoplay stopped at the boss transition for review.",
                            state_fingerprint=snapshot.state_fingerprint,
                            state_after={"state_type": boss_after.state_type, "act": boss_after_act,
                                         "state_fingerprint": boss_after.state_fingerprint},
                        )
                        return {
                            "status": "first_boss_defeated",
                            "reason": "The complete game state left Act 1 boss combat; stopped before any later-room action.",
                            "last_state_type": boss_after.state_type,
                            "steps": steps,
                        }
            if after.get("complete") is not True:
                try:
                    settled = await self._wait_for_settled_state(timeout_seconds=45.0)
                except GameApiError as read_error:
                    return {"status": "paused", "reason": f"The action was submitted once, but its state transition could not be refreshed: {read_error}", "last_state_type": after.get("state_type"), "steps": steps}
                if settled.complete:
                    settled_info = {
                        "state_type": settled.state_type,
                        "state_fingerprint": settled.state_fingerprint,
                        "complete": True,
                        "errors": list(settled.errors),
                    }
                    self._autoplay_next_snapshot = settled
                    self._last_autoplay_submission = {
                        "action_id": (receipt.get("action") or {}).get("action_id"),
                        "submitted_state_fingerprint": proposal.get("state_fingerprint"),
                        "state_fingerprint": settled.state_fingerprint,
                        "state_type": settled.state_type,
                        "proposal_id": proposal.get("proposal_id"),
                    }
                    steps[-1]["state_after"] = settled_info
                    self.events.record(
                        "autoplay_state_settled", provider=receipt.get("provider", "none"),
                        state_type=snapshot.state_type, game_version=snapshot.game_version,
                        status="refreshed_after_transition", action_id=(receipt.get("action") or {}).get("action_id"),
                        reason="The submitted game action was not replayed; a later read-only MCP poll returned a complete state.",
                        state_fingerprint=proposal.get("state_fingerprint"),
                        state_after=settled_info,
                    )
                    continue
                if settled.state_type in TERMINAL_SCREEN_TYPES:
                    outcome = "victory" if settled.state_type in {"victory", "run_complete", "campaign_complete"} else "defeat"
                    terminal_info = {
                        "state_type": settled.state_type,
                        "state_fingerprint": settled.state_fingerprint,
                        "complete": settled.complete,
                        "errors": list(settled.errors),
                    }
                    self.events.record(
                        "run_terminal_state", provider="none", state_type=settled.state_type,
                        game_version=settled.game_version, status=outcome,
                        reason="A submitted action reached a terminal game screen; no action was replayed.",
                        state_fingerprint=settled.state_fingerprint, state_after=terminal_info,
                    )
                    self.events.observe_state(
                        settled.raw, settled.game_version,
                        actions_enabled=self.settings.game_actions_enabled, character=settled.character,
                    )
                    return {"status": "run_ended", "reason": f"Run reached terminal screen {settled.state_type}.", "last_state_type": settled.state_type, "steps": steps}
                return {"status": "paused", "reason": "The last action was submitted once, but the resulting state is incomplete. Read state before another action.", "last_state_type": after.get("state_type"), "steps": steps}
        return {"status": "batch_complete", "reason": "Batch limit reached; the next decision is ready to start immediately.", "steps": steps}

    @staticmethod
    def _map_graph_fingerprint(map_data: dict[str, Any], act: Any = None) -> str:
        if not isinstance(map_data, dict):
            return ""
        return fingerprint({"act": act, **{key: map_data.get(key) for key in ("nodes", "boss", "bosses")}})

    def _restore_route(self, snapshot: GameSnapshot, graph_fingerprint: str) -> str | None:
        map_data = snapshot.raw.get("map", {})
        current = map_data.get("current_position", {})
        if not isinstance(current, dict):
            return None
        current_coord = (current.get("col"), current.get("row"))
        current_options = {
            (node.get("col"), node.get("row"))
            for node in map_data.get("next_options", []) if isinstance(node, dict)
        }
        for event in self.events.recent_routes():
            context = event.get("state_context") if isinstance(event.get("state_context"), dict) else {}
            planned_map = context.get("map") if isinstance(context.get("map"), dict) else {}
            planned_run = context.get("run") if isinstance(context.get("run"), dict) else {}
            stored_fingerprint = event.get("map_graph_fingerprint") or self._map_graph_fingerprint(
                planned_map, planned_run.get("act")
            )
            if stored_fingerprint != graph_fingerprint:
                continue
            try:
                route = self._validate_route(planned_map, event.get("route"))
            except DecisionPaused:
                continue
            origin = planned_map.get("current_position", {})
            origin_coord = (origin.get("col"), origin.get("row")) if isinstance(origin, dict) else (None, None)
            cursor = 0 if current_coord == origin_coord else next(
                (index + 1 for index, node in enumerate(route) if (node["col"], node["row"]) == current_coord),
                -1,
            )
            if cursor < 0:
                continue
            if cursor < len(route) and (route[cursor]["col"], route[cursor]["row"]) not in current_options:
                continue
            route_id = uuid4().hex[:12]
            self._routes[route_id] = {
                "route_id": route_id, "route": route, "cursor": cursor,
                "created_at": time.monotonic(), "provider": event.get("provider", "gpt-6-luna"),
                "confidence": event.get("confidence", 0.0), "reason": event.get("reason", "Restored a previously validated route."),
                "source_fingerprint": event.get("state_fingerprint", ""),
                "map_graph_fingerprint": graph_fingerprint,
            }
            return route_id
        return None

    @staticmethod
    def _card_selection_fingerprint(raw: dict[str, Any]) -> str:
        selection = raw.get("card_select", {})
        cards = selection.get("cards", []) if isinstance(selection, dict) else []
        return fingerprint({
            "screen_type": selection.get("screen_type") if isinstance(selection, dict) else None,
            "prompt": selection.get("prompt") if isinstance(selection, dict) else None,
            "cards": [{"index": card.get("index"), "id": card.get("id")} for card in cards if isinstance(card, dict)],
        })

    def _restore_selected_cards(self, selection_key: str) -> set[int]:
        events = self.events.current_run_events()
        start = 0
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            after = event.get("state_after") if isinstance(event.get("state_after"), dict) else {}
            if event.get("state_type") != "card_select" and after.get("state_type") == "card_select":
                start = index + 1
                break
        selected: set[int] = set()
        for event in events[start:]:
            if event.get("state_type") != "card_select":
                continue
            context = event.get("state_context") if isinstance(event.get("state_context"), dict) else {}
            screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
            if self._card_selection_fingerprint({"card_select": screen.get("card_select", {})}) != selection_key:
                continue
            action_id = event.get("action_id") or ""
            if event.get("event_type") == "action_submitted" and action_id.startswith("deck-card:"):
                try:
                    card_index = int(action_id.split(":", 1)[1])
                except ValueError:
                    continue
                if card_index in selected:
                    selected.remove(card_index)
                else:
                    selected.add(card_index)
            elif event.get("event_type") == "action_submitted" and action_id in {
                "confirm-deck-selection", "cancel-deck-selection"
            }:
                selected.clear()
        return selected

    def _track_card_selection(self, pending: dict[str, Any]) -> None:
        context = pending.get("state_context") if isinstance(pending.get("state_context"), dict) else {}
        screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
        selection = screen.get("card_select") if isinstance(screen.get("card_select"), dict) else {}
        key = self._card_selection_fingerprint({"card_select": selection})
        if key != self._card_selection_key:
            self._card_selection_key = key
            self._selected_card_indices = self._restore_selected_cards(key)
        try:
            index = int(pending["action"].payload["index"])
        except (KeyError, TypeError, ValueError):
            return
        if index in self._selected_card_indices:
            self._selected_card_indices.remove(index)
        else:
            self._selected_card_indices.add(index)

    def _restore_last_autoplay_submission(self) -> dict[str, Any] | None:
        for event in reversed(self.events.current_run_events()):
            if event.get("event_type") != "action_submitted":
                continue
            after = event.get("state_after") if isinstance(event.get("state_after"), dict) else {}
            if after.get("complete") is True and after.get("state_fingerprint") and event.get("action_id"):
                self._last_autoplay_submission = {
                    "action_id": event["action_id"],
                    "submitted_state_fingerprint": event.get("state_fingerprint"),
                    "state_fingerprint": after["state_fingerprint"],
                    "state_type": after.get("state_type"),
                    "proposal_id": event.get("proposal_id"),
                }
                return self._last_autoplay_submission
        return None

    @staticmethod
    def _last_submission_made_no_progress(
        last_action: dict[str, Any] | None, state_fingerprint: str
    ) -> bool:
        """Detect unchanged actions except card-select toggles tracked by the run log."""
        if not last_action or not state_fingerprint:
            return False
        if (
            last_action.get("state_type") == "card_select"
            and str(last_action.get("action_id", "")).startswith("deck-card:")
        ):
            # The Mod does not serialize selected deck-card indices into its
            # state fingerprint. _restore_selected_cards reconstructs these
            # toggles from accepted action_submitted events and filters them
            # from the next legal candidate list, so identical fingerprints are
            # expected until the required selection count is reached.
            return False
        submitted = last_action.get("submitted_state_fingerprint")
        settled = last_action.get("state_fingerprint")
        return submitted == settled == state_fingerprint

    @staticmethod
    def _is_full_potion_item_noop(
        snapshot: GameSnapshot,
        last_action: dict[str, Any] | None,
        legal_actions: list[GameAction],
        legal_errors: list[str],
    ) -> bool:
        """Recover only when a potion claim/purchase is proven impossible by full slots."""
        if (
            not last_action
            or legal_errors
            or snapshot.state_type not in {"rewards", "shop", "fake_merchant"}
        ):
            return False
        action_id = last_action.get("action_id")
        if not isinstance(action_id, str):
            return False
        if snapshot.state_type == "rewards":
            prefix = "reward:"
            list_key = "items"
            category_key = "type"
        else:
            prefix = "shop-item:"
            list_key = "items"
            category_key = "category"
        if not action_id.startswith(prefix):
            return False
        index_text = action_id.removeprefix(prefix)
        if not index_text.isdigit():
            return False
        index = int(index_text)
        if snapshot.state_type == "rewards":
            screen_data = snapshot.raw.get("rewards")
        elif snapshot.state_type == "fake_merchant":
            merchant = snapshot.raw.get("fake_merchant")
            screen_data = merchant.get("shop") if isinstance(merchant, dict) else None
        else:
            screen_data = snapshot.raw.get("shop")
        items = screen_data.get(list_key) if isinstance(screen_data, dict) else None
        if not isinstance(items, list):
            return False
        item = next((entry for entry in items if isinstance(entry, dict) and entry.get("index") == index), None)
        if not isinstance(item, dict) or str(item.get(category_key, "")).casefold() != "potion":
            return False
        player = snapshot.raw.get("player")
        potions = player.get("potions") if isinstance(player, dict) else None
        max_slots = player.get("max_potion_slots") if isinstance(player, dict) else None
        if not isinstance(potions, list) or not isinstance(max_slots, int) or max_slots < 0:
            return False
        if len(potions) < max_slots:
            return False
        # The failed item must be absent from the freshly-derived legal actions.
        return not any(action.action_id == action_id for action in legal_actions)

    @staticmethod
    def _safe_route_candidate(candidate: Any) -> list[dict[str, Any]]:
        if not isinstance(candidate, list):
            return []
        return [
            {key: item[key] for key in ("col", "row", "type") if key in item and isinstance(item[key], (str, int))}
            for item in candidate[:40] if isinstance(item, dict)
        ]

    def _decision_provider(self, snapshot: GameSnapshot) -> tuple[Any, float]:
        if self.settings.provider_mode == "mock":
            return self.mock, self.settings.kev_combat_min_confidence
        if self.settings.provider_mode != "live":
            raise DecisionPaused(f"Unknown agent.provider_mode {self.settings.provider_mode!r}; use mock or live.")
        if snapshot.state_type in COMBAT_SCREENS or snapshot.state_type in {"hand_select", "map"}:
            return self.kev, self.settings.kev_combat_min_confidence
        return self.luna, self.settings.luna_min_confidence

    @staticmethod
    def _screen_item_at(context: dict[str, Any], key: str, index: Any) -> dict[str, Any] | None:
        if not isinstance(index, int):
            return None
        screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
        node: Any = screen.get(key)
        if key == "shop" and not isinstance(node, dict):
            merchant = screen.get("fake_merchant")
            node = merchant.get("shop") if isinstance(merchant, dict) else None
        entries: Any = node
        if isinstance(node, dict):
            list_key = {
                "card_reward": "cards", "shop": "items", "treasure": "relics", "relic_select": "relics",
            }.get(key)
            entries = node.get(list_key) if list_key else None
        if not isinstance(entries, list):
            return None
        indexed = next((entry for entry in entries if isinstance(entry, dict) and entry.get("index") == index), None)
        if indexed is not None:
            return indexed
        return entries[index] if 0 <= index < len(entries) and isinstance(entries[index], dict) else None

    @classmethod
    def _confirmed_item_for_action(cls, pending: dict[str, Any], action: GameAction) -> tuple[str, dict[str, Any]] | None:
        context = pending.get("state_context") if isinstance(pending.get("state_context"), dict) else {}
        index = action.payload.get("card_index", action.payload.get("index"))
        entry: dict[str, Any] | None = None
        kind: str | None = None
        if action.action == "select_card_reward":
            entry = cls._screen_item_at(context, "card_reward", index)
            kind = "cards"
        elif action.action == "claim_treasure_relic":
            entry = cls._screen_item_at(context, "treasure", index)
            kind = "relics"
        elif action.action == "select_relic":
            entry = cls._screen_item_at(context, "relic_select", index)
            kind = "relics"
        elif action.action == "shop_purchase":
            entry = cls._screen_item_at(context, "shop", index)
            if isinstance(entry, dict):
                category = str(entry.get("category", "")).casefold()
                if "card" in category or entry.get("card_name") or entry.get("card_id"):
                    kind = "cards"
                elif "relic" in category or entry.get("relic_name") or entry.get("relic_id"):
                    kind = "relics"
        if not isinstance(entry, dict) or kind is None:
            return None
        if kind == "cards":
            item_id = entry.get("id", entry.get("card_id"))
            name = entry.get("name", entry.get("card_name"))
        else:
            item_id = entry.get("id", entry.get("relic_id"))
            name = entry.get("name", entry.get("relic_name"))
        name = name if isinstance(name, str) and name.strip() else None
        item_id = item_id if isinstance(item_id, str) and item_id.strip() else name
        if not isinstance(item_id, str) or not item_id:
            return None
        return kind, {"id": item_id, "name": name or item_id}

    def _commit_luna_context(self, pending: dict[str, Any], action: GameAction) -> None:
        decision = pending.get("decision")
        if not isinstance(decision, Decision) or decision.provider != "gpt-6-luna":
            return
        context = pending.get("state_context") if isinstance(pending.get("state_context"), dict) else {}
        run = context.get("run") if isinstance(context.get("run"), dict) else {}
        state_type = str(pending.get("state_type", "unknown"))
        action_id = action.action_id
        luna_context = self.events.update_luna_context(
            decision.luna_context_update, source=decision.provider, state_type=state_type,
            floor=run.get("floor") if isinstance(run.get("floor"), int) else None,
            action_id=action_id,
        )
        confirmed = self._confirmed_item_for_action(pending, action)
        if confirmed is not None:
            kind, item = confirmed
            luna_context = self.events.confirm_luna_item(
                kind, item, state_type=state_type,
                floor=run.get("floor") if isinstance(run.get("floor"), int) else None,
                action_id=action_id,
            )
        if decision.luna_context_update or confirmed is not None:
            self.events.record(
                "luna_context_updated", provider=decision.provider, state_type=state_type,
                game_version=pending.get("game_version"), status="accepted_luna_context",
                action_id=action_id,
                reason="Committed Luna's bounded strategy update and any selected card/relic fact after the game accepted the legal action.",
                state_context={"luna_context": luna_context}, state_fingerprint=pending.get("state_fingerprint"),
                proposal_id=pending.get("proposal_id"),
            )

    @staticmethod
    def _deterministic_action(snapshot: GameSnapshot, actions: list[GameAction]) -> GameAction | None:
        """Avoid model calls for forced controls, routine collection, and unique legal actions."""
        if snapshot.state_type == "hand_select":
            selection = snapshot.raw.get("hand_select", {})
            if isinstance(selection, dict) and selection.get("can_confirm") is True:
                explicitly_selected = selection.get("selected_cards")
                if isinstance(explicitly_selected, list):
                    selected_count = len(explicitly_selected)
                else:
                    # The Mod exposes active selectable cards but may omit its
                    # selected-card container. Compare it with the complete live
                    # hand so a selected card is not offered to KEV a second time.
                    player = snapshot.raw.get("player", {})
                    full_hand = player.get("hand", []) if isinstance(player, dict) else []
                    selectable = selection.get("cards", [])

                    def card_key(card: Any) -> tuple[Any, ...]:
                        if not isinstance(card, dict):
                            return ()
                        return tuple(card.get(key) for key in ("id", "is_upgraded", "cost", "description"))

                    selected_count = max(
                        0,
                        sum(Counter(map(card_key, full_hand)).values())
                        - sum(Counter(map(card_key, selectable)).values()),
                    ) if isinstance(full_hand, list) and isinstance(selectable, list) else 0
                if selected_count > 0:
                    return next(
                        (action for action in actions if action.action == "combat_confirm_selection"),
                        None,
                    )
        if snapshot.state_type == "card_select":
            selection = snapshot.raw.get("card_select", {})
            if (
                isinstance(selection, dict)
                and selection.get("screen_type") == "NDeckEnchantSelectScreen"
                and selection.get("can_confirm") is True
            ):
                return next((action for action in actions if action.action == "confirm_selection"), None)
        if snapshot.state_type == "event" and snapshot.raw.get("event", {}).get("in_dialogue") is True:
            if len(actions) != 1:
                raise DecisionPaused("Event dialogue state did not reduce to one deterministic action.")
            return actions[0]
        if snapshot.state_type == "rewards":
            # Claims are collected one at a time; card selection is a separate screen
            # and is routed to Luna after card_reward appears.
            return next((action for action in actions if action.action == "claim_reward"), None) or next(
                (action for action in actions if action.action == "proceed"), None
            )
        if len(actions) == 1:
            return actions[0]
        return None

    def _context_for_provider(
        self,
        snapshot: GameSnapshot,
        actions: list[GameAction],
        route_id: str | None,
        combat_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = snapshot.raw
        state_type = snapshot.state_type
        if state_type in COMBAT_SCREENS or state_type == "hand_select":
            player = raw.get("player", {})
            player_context = {key: player.get(key) for key in (
                "character", "hp", "max_hp", "block", "gold", "energy", "max_energy",
                "stars", "hand", "draw_pile_count", "discard_pile_count", "exhaust_pile_count",
                "draw_pile", "discard_pile", "exhaust_pile", "status", "relics", "potions",
            ) if key in player}
            if snapshot.character:
                player_context["character"] = snapshot.character
            context = {
                "game_version": snapshot.game_version,
                "state_type": state_type,
                "run": raw.get("run"),
                "player": player_context,
                "battle": raw.get("battle"),
                "hand_select": raw.get("hand_select"),
                "combat_pressure": combat_pressure(raw),
                "combat_policy": combat_policy,
                "tactical_order": [
                    "Use the complete-line Pareto frontier. Never subtract player HP loss from enemy HP damage with a fixed 1:1 exchange rate. Exact facts are separate axes; send non-dominated attack-versus-defense trades to KEV with their outcomes.",
                    "Hard safety rule: discard a known-lethal exact line when any exact line survives. If every exact line is known lethal, preserve the minimum uncapped-HP-loss option and check unresolved rescue actions.",
                    "Attack value is actual enemy HP removed after Block. A nonlethal hit does not cancel the current intent; only a kill or modeled secondary effect changes this turn's threat. Enemy Block removed stays a separate value.",
                    "A zero-HP-damage attack that only clears enemy Block is removed when the exact full affordable line still deals no HP damage; an affordable but ineffective follow-up is not proof of conversion. Unknown actions remain separate branches.",
                    "Block value is actual incoming HP loss prevented this turn; excess Block has no assumed value without a modeled retention/conversion effect. Do not choose a zero-HP-damage, no-effect attack when a line prevents current damage.",
                    "Temporary or persistent Strength/setup counts only its measured increase to enemy HP damage on the same affordable current-turn line, after each target's Block and per-hit Strength. A Strength-only action without verified same-turn HP-damage increase is filtered locally; visible future piles are audit-only and never justify spending this turn's action.",
                    "On Ascension 0-2, effective damage is a run-level drafting and tempo plan, not a default action for each turn. Each exact line compares post-turn HP with its remaining unblocked visible attack damage; if HP is no greater than that same-threat amount, the line is marked critical. Usable only means the line is outside this current-threat stress marker; it is not an attack bonus or permission to trade HP freely. This is a conservative stress proxy, not a next-turn forecast.",
                    "Use current HP, enemy remaining HP, intents, actual HP damage prevented by Block, kills, and supported status effects to choose among genuine Pareto tradeoffs. Material Block can be better than small nonlethal damage; damage can be better when threat is light and it materially shortens or ends the fight. Do not force either side based on reserve band alone. Do not reward card type, attack count, Strength count, clearing the hand, or spending all Energy by itself. A zero-damage attack with no conversion or confirmed effect is never useful aggression.",
                    "Unmodeled actions remain independent unresolved branches. They do not make the simulated effects of another exact line uncertain; do not invent outcomes for them. Re-read the complete live state after every accepted action.",
                ],
            }
        elif state_type == "map":
            context = {
                "game_version": snapshot.game_version,
                "state_type": state_type,
                "character": snapshot.character,
                "run": raw.get("run"),
                "map": raw.get("map"),
                "route_id": route_id,
                "planned_next": self._routes.get(route_id, {}).get("route", [None])[self._routes.get(route_id, {}).get("cursor", 0)] if route_id in self._routes else None,
            }
        else:
            context = {
                "game_version": snapshot.game_version,
                "state_type": state_type,
                "run": raw.get("run"),
                "player": self._strategic_player_context(raw, snapshot.character),
                "screen": {key: value for key, value in raw.items() if key not in {"player", "run", "battle"}},
            }
            if state_type == "card_select":
                context["selection_progress"] = {
                    "already_selected_indices": sorted(self._selected_card_indices),
                    "note": "The bridge omits selected flags for grid cards; repeated toggle IDs are removed from this step's candidates.",
                }
        context["legal_action_ids"] = [action.action_id for action in actions]
        return context

    @staticmethod
    def _has_complete_live_deck(context: dict[str, Any]) -> bool:
        player = context.get("player")
        deck = player.get("deck") if isinstance(player, dict) else None
        return isinstance(deck, list) and len(deck) > 0 and all(
            isinstance(card, dict)
            and isinstance(card.get("id"), str) and bool(card["id"].strip())
            and isinstance(card.get("name"), str) and bool(card["name"].strip())
            and isinstance(card.get("type"), str) and bool(card["type"].strip())
            and isinstance(card.get("is_upgraded"), bool)
            for card in deck
        )

    @staticmethod
    def _strategic_player_context(raw: dict[str, Any], canonical_character: str | None = None) -> dict[str, Any]:
        player = raw.get("player", {})
        keys = ("character", "hp", "max_hp", "block", "gold", "status", "relics", "potions", "hand", "draw_pile", "discard_pile", "exhaust_pile", "deck")
        context = {key: player.get(key) for key in keys if key in player}
        if canonical_character:
            context["character"] = canonical_character
        return context

    def _require_snapshot(self, snapshot: GameSnapshot) -> None:
        if not snapshot.complete:
            raise DecisionPaused(" ".join(snapshot.errors) or "Game state is incomplete.")
        if self.settings.require_ironclad and snapshot.character:
            if snapshot.character.casefold() not in {"ironclad", "the ironclad", "铁甲战士"}:
                raise DecisionPaused(f"Current character is not Ironclad: {snapshot.character}.")

    async def _pace(self) -> None:
        gap = self.settings.min_action_interval_ms / 1000.0
        remaining = gap - (time.monotonic() - self._last_action_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_action_at = time.monotonic()

    async def _wait_for_settled_state(
        self,
        timeout_seconds: float = 6.0,
        poll_seconds: float = 0.2,
        *,
        wait_for_state_type_change_from: str | None = None,
    ) -> GameSnapshot:
        """Poll read-only state briefly after a submitted action; never repeat the action POST."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                snapshot = await self.snapshot()
            except GameApiError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
                continue
            state_transition_pending = (
                wait_for_state_type_change_from is not None
                and snapshot.state_type == wait_for_state_type_change_from
            )
            if snapshot.state_type in TERMINAL_SCREEN_TYPES or (snapshot.complete and not state_transition_pending):
                return snapshot
            if time.monotonic() >= deadline:
                return snapshot
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    async def _wait_for_actionable_state(
        self,
        timeout_seconds: float = 45.0,
        poll_seconds: float = 0.2,
    ) -> GameSnapshot:
        """Poll complete MCP snapshots until the current phase exposes legal actions."""
        deadline = time.monotonic() + timeout_seconds
        latest: GameSnapshot | None = None
        while True:
            try:
                latest = await self.snapshot()
            except GameApiError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
                continue
            if latest.complete:
                actions, errors = derive_actions(latest)
                if actions and not errors:
                    return latest
            if time.monotonic() >= deadline:
                if latest is not None:
                    return latest
                raise GameApiError("No MCP snapshot became available during the combat phase wait.")
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _validate_route(map_data: dict[str, Any], candidate: Any) -> list[dict[str, Any]]:
        if not isinstance(candidate, list) or not candidate or len(candidate) > 30:
            raise DecisionPaused("Route must contain 1-30 map nodes.")
        nodes = [node for node in map_data.get("nodes", []) if isinstance(node, dict)]
        by_coord = {(n.get("col"), n.get("row")): n for n in nodes if isinstance(n.get("col"), int) and isinstance(n.get("row"), int)}
        current = map_data.get("current_position", {})
        options = {(n.get("col"), n.get("row")) for n in map_data.get("next_options", []) if isinstance(n, dict)}
        boss_data = map_data.get("boss")
        bosses = map_data.get("bosses", [])
        boss_coords = {(n.get("col"), n.get("row")) for n in bosses if isinstance(n, dict)}
        if isinstance(boss_data, dict):
            boss_coords.add((boss_data.get("col"), boss_data.get("row")))
        if not isinstance(current, dict) or not options or not boss_coords:
            raise DecisionPaused("Map graph is missing current position, reachable nodes, or boss coordinates.")
        route: list[dict[str, Any]] = []
        for item in candidate:
            if not isinstance(item, dict) or not isinstance(item.get("col"), int) or not isinstance(item.get("row"), int):
                raise DecisionPaused("Route contains a node without integer coordinates.")
            coord = (item["col"], item["row"])
            if coord not in by_coord and coord not in boss_coords:
                raise DecisionPaused(f"Route contains unknown map node {coord}.")
            node = by_coord.get(coord, {})
            route.append({"col": coord[0], "row": coord[1], "type": str(node.get("type", item.get("type", "Boss" if coord in boss_coords else "unknown")))})
        first = (route[0]["col"], route[0]["row"])
        if first not in options:
            raise DecisionPaused("Route begins with a node that is not currently reachable.")
        for index in range(1, len(route)):
            previous_node = route[index - 1]
            node = route[index]
            previous = (previous_node["col"], previous_node["row"])
            coord = (node["col"], node["row"])
            parent = by_coord.get(previous)
            children = parent.get("children", []) if isinstance(parent, dict) else []
            if not any(isinstance(pair, list) and len(pair) == 2 and tuple(pair) == coord for pair in children):
                raise DecisionPaused(f"Route contains a non-edge from {previous} to {coord}.")
        last = (route[-1]["col"], route[-1]["row"])
        if last not in boss_coords:
            raise DecisionPaused("Route does not finish at a currently listed boss node.")
        return route
