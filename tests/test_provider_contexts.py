from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sts2_agent.config import Settings
from sts2_agent.controller import Controller
from sts2_agent.game_api import GameApiError
from sts2_agent.models import Decision, DecisionPaused, GameAction, GameSnapshot
from sts2_agent.providers.kev import KevStructuredProvider
from sts2_agent.providers.luna import LunaChatCompletionsProvider
from sts2_agent.providers.base import ProviderResponseError, RetryableProviderError


PROJECT = Path(__file__).resolve().parents[1]


class CardSelectionProgressTests(unittest.TestCase):
    def test_selection_toggle_with_same_fingerprint_is_not_misreported_as_no_progress(self):
        last_action = {
            "action_id": "deck-card:4",
            "state_type": "card_select",
            "submitted_state_fingerprint": "same",
            "state_fingerprint": "same",
        }
        self.assertFalse(Controller._last_submission_made_no_progress(last_action, "same"))

    def test_unchanged_ordinary_action_still_trips_no_progress_guard(self):
        last_action = {
            "action_id": "card:0",
            "state_type": "monster",
            "submitted_state_fingerprint": "same",
            "state_fingerprint": "same",
        }
        self.assertTrue(Controller._last_submission_made_no_progress(last_action, "same"))


class HandSelectionProgressTests(unittest.TestCase):
    def test_confirms_once_a_selected_hand_card_is_visible_by_set_difference(self):
        raw = {
            "hand_select": {
                "mode": "upgrade_select",
                "cards": [{"id": "EVIL_EYE", "name": "邪眼", "type": "Skill", "cost": "1", "description": "获得8点格挡。", "is_upgraded": False, "index": 0}],
                "can_confirm": True,
            },
            "player": {"hand": [
                {"id": "FIEND_FIRE", "name": "恶魔之焰", "type": "Attack", "cost": "2", "description": "消耗所有手牌。", "is_upgraded": False, "index": 0},
                {"id": "EVIL_EYE", "name": "邪眼", "type": "Skill", "cost": "1", "description": "获得8点格挡。", "is_upgraded": False, "index": 1},
            ]},
        }
        snapshot = GameSnapshot("hand_select", raw, "fp", "v0.107.1", "Ironclad", True)
        actions = [
            GameAction("hand-select:0", "combat_select_card", "combat-selection", "Select Evil Eye", {"action": "combat_select_card", "card_index": 0}),
            GameAction("confirm-hand-selection", "combat_confirm_selection", "combat-selection", "Confirm selection", {"action": "combat_confirm_selection"}),
        ]

        selected = Controller._deterministic_action(snapshot, actions)

        self.assertEqual(selected.action_id, "confirm-hand-selection")

    def test_does_not_confirm_when_no_hand_card_is_selected(self):
        card = {"id": "EVIL_EYE", "name": "邪眼", "type": "Skill", "cost": "1", "description": "获得8点格挡。", "is_upgraded": False}
        raw = {
            "hand_select": {"mode": "upgrade_select", "cards": [dict(card, index=0)], "can_confirm": True},
            "player": {"hand": [dict(card, index=1)]},
        }
        snapshot = GameSnapshot("hand_select", raw, "fp", "v0.107.1", "Ironclad", True)
        actions = [
            GameAction("hand-select:0", "combat_select_card", "combat-selection", "Select Evil Eye", {"action": "combat_select_card", "card_index": 0}),
            GameAction("confirm-hand-selection", "combat_confirm_selection", "combat-selection", "Confirm selection", {"action": "combat_confirm_selection"}),
        ]

        self.assertIsNone(Controller._deterministic_action(snapshot, actions))


class KevTimeoutRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_kev_failure_retries_only_after_same_complete_state_read(self):
        temp, env = temporary_logs()
        snapshot = GameSnapshot("monster", {}, "same-fp", "v0.107.1", "Ironclad", True)

        class FlakyKev:
            name = "kev"

            def __init__(self):
                self.calls = 0

            async def choose(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RetryableProviderError("KEV request timed out; no game action was submitted.")
                return Decision("end-turn", "kev", 1.0, "test retry success")

        try:
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(return_value=snapshot)
                provider = FlakyKev()
                action = GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"})
                decision = await controller._choose_with_verified_retry(
                    provider, state={}, actions=[action], min_confidence=0.0,
                    original_snapshot=snapshot,
                )
            self.assertEqual(decision.action_id, "end-turn")
            self.assertEqual(provider.calls, 2)
            controller.snapshot.assert_awaited_once()
        finally:
            temp.cleanup()

    async def test_transient_luna_failure_retries_only_after_same_complete_state_read(self):
        temp, env = temporary_logs()
        snapshot = GameSnapshot("rest_site", {}, "same-fp", "v0.107.1", "Ironclad", True)

        class FlakyLuna:
            name = "gpt-6-luna"

            def __init__(self):
                self.calls = 0

            async def choose(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RetryableProviderError("GPT-6 Luna request timed out; the game action was not submitted.")
                return Decision("rest-option:0", "gpt-6-luna", 0.75, "test retry success")

        try:
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(return_value=snapshot)
                provider = FlakyLuna()
                action = GameAction("rest-option:0", "choose_rest_option", "rest-site", "Rest", {"index": 0}, True)
                decision = await controller._choose_with_verified_retry(
                    provider, state={}, actions=[action], min_confidence=0.6,
                    original_snapshot=snapshot,
                )
            self.assertEqual(decision.action_id, "rest-option:0")
            self.assertEqual(provider.calls, 2)
            controller.snapshot.assert_awaited_once()
        finally:
            temp.cleanup()

    async def test_luna_timeout_retry_aborts_when_live_state_changes(self):
        temp, env = temporary_logs()
        original = GameSnapshot("rest_site", {}, "before-fp", "v0.107.1", "Ironclad", True)
        changed = GameSnapshot("map", {}, "after-fp", "v0.107.1", "Ironclad", True)

        class FlakyLuna:
            name = "gpt-6-luna"
            calls = 0

            async def choose(self, **_kwargs):
                self.calls += 1
                raise RetryableProviderError("GPT-6 Luna request timed out; the game action was not submitted.")

        try:
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(return_value=changed)
                provider = FlakyLuna()
                with self.assertRaisesRegex(DecisionPaused, "state changed after GPT-6 Luna timed out"):
                    await controller._choose_with_verified_retry(
                        provider, state={}, actions=[], min_confidence=0.6,
                        original_snapshot=original,
                    )
            self.assertEqual(provider.calls, 1)
        finally:
            temp.cleanup()


class ControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_through_complete_but_non_actionable_combat_snapshot(self):
        temp, env = temporary_logs()
        transient = GameSnapshot("monster", {}, "enemy-phase", "v0.107.1", "Ironclad", True)
        ready = GameSnapshot("monster", {}, "player-phase", "v0.107.1", "Ironclad", True)
        action = GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"})
        try:
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(side_effect=[transient, ready])
                with patch(
                    "sts2_agent.controller.derive_actions",
                    side_effect=[([], ["Combat is not in a ready player action phase."]), ([action], [])],
                ):
                    settled = await controller._wait_for_actionable_state(timeout_seconds=1.0, poll_seconds=0.0)
            self.assertEqual(settled.state_fingerprint, "player-phase")
            self.assertEqual(controller.snapshot.await_count, 2)
        finally:
            temp.cleanup()

    async def test_changed_state_aborts_retry_without_another_provider_call(self):
        temp, env = temporary_logs()
        original = GameSnapshot("monster", {}, "before-fp", "v0.107.1", "Ironclad", True)
        changed = GameSnapshot("rewards", {}, "after-fp", "v0.107.1", "Ironclad", True)

        class FlakyKev:
            name = "kev"
            calls = 0

            async def choose(self, **_kwargs):
                self.calls += 1
                raise RetryableProviderError("KEV request timed out; no game action was submitted.")

        try:
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(return_value=changed)
                provider = FlakyKev()
                with self.assertRaisesRegex(DecisionPaused, "state changed after KEV timed out"):
                    await controller._choose_with_verified_retry(
                        provider, state={}, actions=[], min_confidence=0.0,
                        original_snapshot=original,
                    )
            self.assertEqual(provider.calls, 1)
        finally:
            temp.cleanup()


class StubResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self):
        return self.payload


class StubAsyncClient:
    request_url = None
    request_json = None
    response_payload = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        type(self).request_url = url
        type(self).request_json = kwargs.get("json")
        return StubResponse(type(self).response_payload)


def configured_settings() -> Settings:
    return replace(
        Settings.load(PROJECT / "config.example.toml"),
        provider_mode="live",
        game_actions_enabled=False,
        luna_min_confidence=0.0,
    )


def temporary_logs():
    temp = tempfile.TemporaryDirectory()
    runs = Path(temp.name) / "runs"
    runs.mkdir()
    env = {
        "STS2_RUN_LOG_DIR": str(runs),
        "STS2_AUDIT_DB": str(Path(temp.name) / "events.sqlite3"),
    }
    return temp, env


class ProviderContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_kev_rejects_luna_context_and_legacy_run_memory(self):
        provider = KevStructuredProvider("http://127.0.0.1:8009", "kev-latest", 1.0)
        action = GameAction("end-turn", "end_turn", "combat", "End turn.")
        for key in ("luna_context", "run_memory"):
            with self.subTest(key=key), self.assertRaises(ProviderResponseError):
                await provider.choose(
                    state={"state_type": "monster", key: {}},
                    actions=[action], min_confidence=0.0,
                )

    async def test_kev_request_contains_only_its_supplied_live_state(self):
        StubAsyncClient.response_payload = {
            "answers": {"action": {
                "choice": "end-turn", "probabilities": {"end-turn": 1.0}, "confidence": 1.0,
            }},
        }
        action = GameAction("end-turn", "end_turn", "combat", "End turn.")
        provider = KevStructuredProvider("http://127.0.0.1:8009", "kev-latest", 1.0)
        live_state = {
            "state_type": "monster",
            "player": {"hand": []},
            "combat_policy": {"action_assessments": [{
                "action_id": "end-turn", "action": "end_turn",
                "visible_hp_loss_if_ending_now": 8,
                "current_turn_continuation": {
                    "best_followup_fixed_attack_hp_damage_estimate": 6,
                    "best_followup_pure_block_estimate": 3,
                    "followup_lines_are_independent_upper_bounds": True,
                },
                "joint_turn_lines": {
                    "basis": "joint_bounded_current_hand_fixed_attacks_pure_blocks_strength_only",
                    "supported": True,
                    "best_player_hp_preservation_line": {
                        "projected_hp_loss_if_turn_ended_estimate": 3,
                        "enemy_hp_damage_estimate": 0,
                    },
                },
            }], "turn_outcome_comparison": {
                "enemy_block_rule": "Enemy Block removed is a state change, not HP damage or a tactical tie-break.",
                "candidates": [{"first_action_id": "end-turn", "current_effect": "end_turn"}],
            }},
        }
        with patch("sts2_agent.providers.kev.httpx.AsyncClient", StubAsyncClient):
            await provider.choose(
                state=live_state,
                actions=[action], min_confidence=0.0,
            )
        request = StubAsyncClient.request_json
        self.assertEqual(StubAsyncClient.request_url, "http://127.0.0.1:8009/v1/systemone")
        self.assertEqual(request["state"], {"state_type": "monster", "player": {"hand": []}})
        self.assertNotIn("luna_context", request["state"])
        self.assertNotIn("run_memory", request["state"])
        criterion = request["questions"]["action"]["criteria"]["end-turn"]
        self.assertIn('"current_effect":"end_turn"', criterion)
        self.assertNotIn("current_turn_continuation", criterion)
        instructions = request["questions"]["action"]["instructions"]
        self.assertIn("按整条本回合线路判断", instructions)
        self.assertIn("攻击、格挡、力量或易伤设置的牌型本身没有分数", instructions)
        self.assertIn("不要把敌方实际生命伤害和我方生命损失按1:1相减", instructions)
        self.assertIn("只有所有精确线都确定致死时", instructions)
        self.assertIn("削敌人格挡单独记账", instructions)
        self.assertIn("不要为了清空手牌或花完能量而出牌", instructions)
        self.assertIn("临时力量必须按整条可执行线路核算", instructions)
        self.assertIn("纯力量牌（如燃烧，或下回合开始才生效的恶魔形态）", instructions)
        self.assertIn("延迟到下一回合的力量绝不能算作本回合攻击增伤", instructions)
        self.assertIn("未来抽到的攻击不能当成当前伤害", instructions)
        self.assertIn("完整线路实际穿过敌人格挡", instructions)
        self.assertIn("未建模药水/卡牌是独立未知候选", instructions)
        self.assertNotIn("净交换值", instructions)
        self.assertNotIn("LUNA-ONLY IRONCLAD SKILL", json.dumps(request, ensure_ascii=False))

    async def test_luna_gets_skill_full_deck_and_its_context(self):
        StubAsyncClient.response_payload = {"choices": [{"message": {"content": json.dumps({
            "action_id": "skip-card-reward", "confidence": 0.9,
            "reason": "The current deck already covers these roles.",
            "luna_context_update": {},
        })}}]}
        actions = [
            GameAction("reward-card:0", "select_card_reward", "card-reward", "Take card A.", {"card_index": 0}, True),
            GameAction("reward-card:1", "select_card_reward", "card-reward", "Take card B.", {"card_index": 1}, True),
            GameAction("reward-card:2", "select_card_reward", "card-reward", "Take card C.", {"card_index": 2}, True),
            GameAction("skip-card-reward", "skip_card_reward", "card-reward", "Skip.", strategic=True),
        ]
        full_deck = [{"id": "IRON_WAVE", "name": "Iron Wave", "type": "Attack", "is_upgraded": False}]
        state = {
            "state_type": "card_reward",
            "player": {"deck": full_deck},
            "luna_context": {"strategy": {"win_condition": "Strength"}, "confirmed_run_items": {"cards": [], "relics": []}},
            "screen": {"card_reward": {"cards": [
                {"id": "CINDER", "name": "Cinder", "index": 0},
                {"id": "IRON_WAVE", "name": "Iron Wave", "index": 1},
                {"id": "BLOODLETTING", "name": "Bloodletting", "index": 2},
            ]}},
            "legal_action_ids": [action.action_id for action in actions],
        }
        provider = LunaChatCompletionsProvider("http://127.0.0.1:18317/v1", "gpt-6-luna", "mock", 1.0)
        with patch("sts2_agent.providers.luna.httpx.AsyncClient", StubAsyncClient):
            decision = await provider.choose(state=state, actions=actions, min_confidence=0.0)
        self.assertEqual(decision.action_id, "skip-card-reward")
        self.assertEqual(decision.luna_context_update, {})
        self.assertEqual(StubAsyncClient.request_url, "http://127.0.0.1:18317/v1/chat/completions")
        body = StubAsyncClient.request_json
        self.assertIn("LUNA-ONLY IRONCLAD SKILL", body["messages"][0]["content"])
        self.assertIn("If none of the three offers has a clear advantage over skip", body["messages"][0]["content"])
        supplied_state = json.loads(body["messages"][1]["content"])["state"]
        self.assertEqual(supplied_state["player"]["deck"], full_deck)
        self.assertIn("luna_context", supplied_state)

    async def test_missing_live_deck_pauses_card_reward_before_luna_call(self):
        temp, env = temporary_logs()
        try:
            raw = {
                "bridge_schema_version": 2,
                "state_type": "card_reward",
                "run": {"act": 1, "floor": 2},
                "player": {"character": "Ironclad", "hp": 60, "max_hp": 80, "gold": 50},
                "card_reward": {
                    "cards": [
                        {"index": 0, "id": "IRON_WAVE", "name": "Iron Wave", "rarity": "Common", "description": ""},
                        {"index": 1, "id": "CINDER", "name": "Cinder", "rarity": "Uncommon", "description": ""},
                    ],
                    "can_skip": True,
                },
            }
            snapshot = GameSnapshot("card_reward", raw, "card-reward-fp", "v0.107.1", "Ironclad", True)
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.luna.choose = AsyncMock()
                with self.assertRaisesRegex(DecisionPaused, "complete current player.deck"):
                    await controller.recommend_next_action(_snapshot=snapshot)
                controller.luna.choose.assert_not_awaited()
        finally:
            temp.cleanup()

    async def test_single_legal_combat_action_skips_both_models(self):
        temp, env = temporary_logs()
        try:
            raw = {
                "bridge_schema_version": 2,
                "state_type": "monster",
                "run": {"act": 1, "floor": 2},
                "player": {"character": "Ironclad", "hp": 60, "max_hp": 80, "hand": [], "potions": []},
                "battle": {"enemies": []},
            }
            snapshot = GameSnapshot("monster", raw, "combat-end-turn-fp", "v0.107.1", "Ironclad", True)
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.kev.choose = AsyncMock()
                controller.luna.choose = AsyncMock()
                proposal = await controller.recommend_next_action(_snapshot=snapshot)

            self.assertEqual(proposal["provider"], "deterministic")
            self.assertEqual(proposal["action"]["action_id"], "end-turn")
            self.assertIn("Exactly one legal action remained", proposal["reason"])
            controller.kev.choose.assert_not_awaited()
            controller.luna.choose.assert_not_awaited()
        finally:
            temp.cleanup()

    async def test_selected_line_continues_after_rebinding_to_fresh_hand_indices(self):
        temp, env = temporary_logs()
        try:
            before_raw = {
                "bridge_schema_version": 2,
                "state_type": "monster",
                "run": {"act": 1, "floor": 4, "ascension": 0},
                "player": {"character": "Ironclad", "hp": 40, "max_hp": 80, "block": 0, "energy": 2,
                           "status": [], "relics": [], "potions": [], "hand": [
                    {"index": 0, "id": "JAB_A", "name": "轻刺", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "can_play": True},
                    {"index": 1, "id": "JAB_B", "name": "重击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "can_play": True},
                ]},
                "battle": {"enemies": [{"entity_id": "E", "name": "敌人", "hp": 30, "block": 6,
                                         "intents": [{"type": "Attack", "label": "4"}]}]},
            }
            after_raw = {
                **before_raw,
                "player": {**before_raw["player"], "energy": 1, "hand": [
                    {"index": 0, "id": "JAB_B", "name": "重击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "can_play": True},
                ]},
                "battle": {"enemies": [{"entity_id": "E", "name": "敌人", "hp": 30, "block": 0,
                                         "intents": [{"type": "Attack", "label": "4"}]}]},
            }
            before = GameSnapshot("monster", before_raw, "line-start", "v0.107.1", "Ironclad", True)
            after = GameSnapshot("monster", after_raw, "line-after-first", "v0.107.1", "Ironclad", True)
            plan = (
                {"kind": "play_card", "card_id": "JAB_A", "card_name": "轻刺", "card_type": "Attack", "is_upgraded": False, "target_id": "E"},
                {"kind": "play_card", "card_id": "JAB_B", "card_name": "重击", "card_type": "Attack", "is_upgraded": False, "target_id": "E"},
            )
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(replace(configured_settings(), game_actions_enabled=True, min_action_interval_ms=0))
                controller.kev.choose = AsyncMock(return_value=Decision(
                    "card:0:target:E", "kev", 0.72, "Choose the complete attack sequence.",
                    {"card:0:target:E": 0.72, "card:1:target:E": 0.18, "end-turn": 0.10},
                    plan_steps=plan, line_choice_id="turn-line:0",
                ))
                proposal = await controller.recommend_next_action(_snapshot=before)
                controller.snapshot = AsyncMock(return_value=before)
                controller.game.act = AsyncMock(return_value={"status": "ok"})
                controller._wait_for_settled_state = AsyncMock(return_value=after)
                receipt = await controller.execute_proposal(proposal["proposal_id"])
                next_proposal = await controller.recommend_next_action(_snapshot=after)

            self.assertTrue(receipt["executed"])
            self.assertEqual(next_proposal["action"]["action_id"], "card:0:target:E")
            self.assertEqual(next_proposal["provider"], "kev")
            self.assertIn("rebound to the freshly read legal action set", next_proposal["reason"])
            controller.kev.choose.assert_awaited_once()
            controller.game.act.assert_awaited_once_with({"action": "play_card", "card_index": 0, "target": "E"})
        finally:
            temp.cleanup()

    async def test_proven_attack_kill_deterministically_removes_dominated_defend_and_end_turn(self):
        temp, env = temporary_logs()
        try:
            raw = {
                "bridge_schema_version": 2,
                "state_type": "monster",
                "run": {"act": 1, "floor": 3, "ascension": 0},
                "player": {
                    "character": "Ironclad", "hp": 72, "max_hp": 80, "block": 0, "energy": 3,
                    "status": [],
                    "relics": [{"id": "BURNING_BLOOD", "name": "Burning Blood", "description": "At the end of combat, heal 6 HP."}],
                    "hand": [
                        {"index": 0, "id": "DEFEND_IRONCLAD", "name": "Defend", "type": "Skill", "cost": "1", "description": "获得5点格挡", "target_type": "Self", "is_upgraded": False, "can_play": True},
                        {"index": 1, "id": "STRIKE_R", "name": "Strike", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "can_play": True},
                        {"index": 2, "id": "STRIKE_R", "name": "Strike", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "can_play": True},
                    ],
                    "potions": [],
                },
                "battle": {"enemies": [{
                    "entity_id": "NIBBIT_0", "name": "Nibbit", "hp": 6, "block": 0,
                    "status": [], "intents": [{"type": "Attack", "label": "8"}],
                }]},
            }
            snapshot = GameSnapshot("monster", raw, "nibbit-lethal-fp", "v0.107.1", "Ironclad", True)
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                action_ids = ["card:1:target:NIBBIT_0"]
                controller.kev.choose = AsyncMock(return_value=Decision(
                    "card:1:target:NIBBIT_0", "kev", 0.6, "Exact kill cancels the visible attack.",
                    {"card:1:target:NIBBIT_0": 1.0},
                ))
                proposal = await controller.recommend_next_action(_snapshot=snapshot)

            self.assertEqual(proposal["provider"], "deterministic")
            self.assertEqual(proposal["action"]["action_id"], "card:1:target:NIBBIT_0")
            controller.kev.choose.assert_not_awaited()
            self.assertIn("Exactly one legal action remained", proposal["reason"])
        finally:
            temp.cleanup()

    async def test_combat_transition_refresh_retries_read_only_after_temporary_bridge_error(self):
        temp, env = temporary_logs()
        try:
            complete = GameSnapshot("rewards", {}, "settled-fp", "v0.107.1", "Ironclad", True)
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.snapshot = AsyncMock(side_effect=[GameApiError("temporary bridge read failure"), complete])
                controller.game.act = AsyncMock()
                settled = await controller._wait_for_settled_state(timeout_seconds=1.0, poll_seconds=0.0)

            self.assertIs(settled, complete)
            self.assertEqual(controller.snapshot.await_count, 2)
            controller.game.act.assert_not_awaited()
        finally:
            temp.cleanup()

    async def test_luna_plans_once_then_route_step_is_deterministic(self):
        temp, env = temporary_logs()
        try:
            raw = {
                "bridge_schema_version": 2,
                "state_type": "map",
                "run": {"act": 1, "floor": 0},
                "player": {
                    "character": "Ironclad", "hp": 70, "max_hp": 80, "gold": 20,
                    "deck": [{"id": "STRIKE_R", "name": "Strike", "type": "Attack", "is_upgraded": False}],
                },
                "map": {
                    "current_position": {"col": 0, "row": 0},
                    "next_options": [{"index": 0, "col": 1, "row": 1, "type": "Boss"}],
                    "nodes": [],
                    "boss": {"col": 1, "row": 1},
                },
            }
            snapshot = GameSnapshot("map", raw, "map-fp", "v0.107.1", "Ironclad", True)
            route_answer = {
                "route": [{"col": 1, "row": 1, "type": "Boss"}], "confidence": 0.99,
                "reason": "Direct route to the boss.", "luna_context_update": {},
            }
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(configured_settings())
                controller.luna.plan_route = AsyncMock(return_value=route_answer)
                controller.luna.choose = AsyncMock()
                route = await controller.plan_map_route(_snapshot=snapshot)
                self.assertIn("luna_context", controller.luna.plan_route.await_args.kwargs["state"])

                action_id = "map-node:1:1"
                controller.kev.choose = AsyncMock()
                proposal = await controller.recommend_next_action(route["route_id"], _snapshot=snapshot)

            controller.luna.plan_route.assert_awaited_once()
            controller.luna.choose.assert_not_awaited()
            controller.kev.choose.assert_not_awaited()
            self.assertEqual(proposal["provider"], "deterministic")
            self.assertEqual(proposal["action"]["action_id"], action_id)
            self.assertIn("deterministically", proposal["reason"])
        finally:
            temp.cleanup()

    async def test_fresh_run_start_uses_one_advertised_menu_action(self):
        temp, env = temporary_logs()
        try:
            main_menu = {
                "bridge_schema_version": 2, "state_type": "menu", "menu_screen": "main",
                "options": ["singleplayer", "multiplayer", "compendium", "timeline", "settings", "quit"],
            }
            singleplayer_menu = {
                "bridge_schema_version": 2, "state_type": "menu", "menu_screen": "singleplayer",
                "options": ["standard", "back"],
            }
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(replace(configured_settings(), game_actions_enabled=True))
                controller._read_passive_bridge_state = AsyncMock(side_effect=[main_menu, main_menu, singleplayer_menu])
                controller.game.act = AsyncMock(return_value={"status": "ok"})
                result = await controller.start_new_ironclad_run_step(profile_id=2)

            self.assertEqual(result["status"], "menu_step_complete")
            controller.game.act.assert_awaited_once_with({"action": "menu_select", "option": "singleplayer"})
        finally:
            temp.cleanup()

    async def test_fresh_run_start_refuses_continue_without_submitting(self):
        temp, env = temporary_logs()
        try:
            main_menu = {
                "bridge_schema_version": 2, "state_type": "menu", "menu_screen": "main",
                "options": ["singleplayer", {"name": "continue", "enabled": True}],
            }
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(replace(configured_settings(), game_actions_enabled=True))
                controller._read_passive_bridge_state = AsyncMock(return_value=main_menu)
                controller.game.act = AsyncMock()
                with self.assertRaisesRegex(DecisionPaused, "resumable run"):
                    await controller.start_new_ironclad_run_step(profile_id=2)

            controller.game.act.assert_not_awaited()
        finally:
            temp.cleanup()

    async def test_fresh_run_start_timeout_is_unknown_and_never_retried(self):
        temp, env = temporary_logs()
        try:
            main_menu = {
                "bridge_schema_version": 2, "state_type": "menu", "menu_screen": "main",
                "options": ["singleplayer"],
            }
            with patch.dict(os.environ, env, clear=False):
                controller = Controller(replace(configured_settings(), game_actions_enabled=True))
                controller._read_passive_bridge_state = AsyncMock(return_value=main_menu)
                controller.game.act = AsyncMock(side_effect=GameApiError("timed out"))
                result = await controller.start_new_ironclad_run_step(profile_id=2)

            self.assertEqual(result["status"], "outcome_unknown")
            self.assertEqual(result["game_action_submitted"], "unknown")
            controller.game.act.assert_awaited_once()
            with self.assertRaisesRegex(DecisionPaused, "not retried"):
                await controller.start_new_ironclad_run_step(profile_id=2)
            controller.game.act.assert_awaited_once()
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
