import json
import unittest
from unittest.mock import patch

from sts2_agent.combat_tactics import combat_candidate_policy
from sts2_agent.models import GameAction
from sts2_agent.providers.kev import KevStructuredProvider, build_systemone_request


class KevRequestContextTests(unittest.TestCase):
    def test_request_keeps_live_combat_facts_and_discards_duplicated_policy_blob(self):
        actions = [
            GameAction("attack-1", "play_card", "combat", "Play Strike; deal 6 damage.", {"card_index": 0}),
            GameAction("defend-1", "play_card", "combat", "Play Defend; gain 5 Block.", {"card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        state = {
            "game_version": "v0.107.1",
            "state_type": "monster",
            "run": {"act": 1, "floor": 2, "ascension": 0, "seed": "ignored"},
            "player": {
                "character": "Ironclad", "hp": 80, "max_hp": 80, "block": 0,
                "energy": 3, "max_energy": 3,
                "hand": [
                    {"index": 0, "id": "STRIKE_IRONCLAD", "name": "Strike", "type": "Attack", "cost": "1", "description": "Deal 6 damage.", "can_play": True, "target_type": "AnyEnemy"},
                    {"index": 1, "id": "DEFEND_IRONCLAD", "name": "Defend", "type": "Skill", "cost": "1", "description": "Gain 5 Block.", "can_play": True},
                ],
                "deck": [{"description": "large omitted pile"}],
                "draw_pile": [{"description": "not needed for this combat request"}],
                "relics": [{"id": "BURNING_BLOOD", "name": "Burning Blood", "description": "Heal 6 after combat.", "counter": None}],
                "potions": [],
                "unrelated_secret": "must not be sent",
            },
            "battle": {
                "round": 1, "turn": "player", "is_play_phase": True,
                "enemies": [{"entity_id": "enemy-1", "name": "Fuzzy Wurm", "hp": 57, "max_hp": 57, "block": 0, "status": [], "intents": [{"type": "Attack", "label": "4"}], "private": "ignored"}],
            },
            "combat_pressure": {"incoming_attack_damage": 4, "current_block": 0, "projected_hp_loss_if_turn_ended_now": 4, "player_hp": 80, "lethal_if_turn_ended_now": False, "ignored": 123},
            "tactical_order": ["large duplicate advice payload"],
            "combat_policy": {
                "combat_strategy": {
                    "profile": "low_ascension_tempo", "ascension": 0,
                    "effective_damage_is_a_run_plan": True,
                    "effective_damage_is_a_turn_action_default": False,
                },
                "action_assessments": [
                    {"action_id": "attack-1", "energy_cost": 1, "current_tactical_effect_class": "enemy_hp_damage", "enemy_hp_damage_estimate": 6, "joint_turn_lines": {"large": "duplicate data"}},
                    {"action_id": "defend-1", "energy_cost": 1, "current_tactical_effect_class": "current_hp_prevention", "current_hp_loss_prevented_by_this_action_estimate": 4},
                    {"action_id": "end-turn", "energy_cost": 0, "current_tactical_effect_class": "end_turn"},
                ],
                "turn_outcome_comparison": {
                    "candidates": [
                        {"first_action_id": "attack-1", "current_effect": "enemy_hp_damage", "direct_enemy_hp_damage": 6, "pareto_tradeoff_lines": [{"line_action_ids": ["attack-1"], "enemy_hp_damage": 6, "player_hp_loss_including_self_cost_uncapped": 4, "survives": True, "uncertain": False}]},
                        {"first_action_id": "defend-1", "current_effect": "current_hp_prevention", "direct_hp_loss_prevented": 4, "pareto_tradeoff_lines": [{"line_action_ids": ["defend-1"], "enemy_hp_damage": 0, "player_hp_loss_including_self_cost_uncapped": 0, "survives": True, "uncertain": False}]},
                        {"first_action_id": "end-turn", "current_effect": "end_turn", "pareto_tradeoff_lines": [{"line_action_ids": ["end-turn"], "enemy_hp_damage": 0, "player_hp_loss_including_self_cost_uncapped": 4, "survives": True, "uncertain": False}]},
                    ]
                },
            },
            "luna_context": None,
        }

        request = build_systemone_request(state=state, actions=actions, model="kev-latest")
        sent_state = request["state"]
        self.assertEqual(request["model"], "kev-latest")
        self.assertEqual(set(request["questions"]["action"]["criteria"]), {a.action_id for a in actions})
        self.assertEqual(sent_state["player"]["hp"], 80)
        self.assertEqual(sent_state["player"]["hand"][0]["name"], "Strike")
        self.assertEqual(sent_state["battle"]["enemies"][0]["intents"][0]["label"], "4")
        self.assertIn("relics", sent_state["player"])
        self.assertEqual(sent_state["run"], {"act": 1, "floor": 2})
        self.assertNotIn("combat_strategy", sent_state)
        self.assertNotIn("deck", sent_state["player"])
        self.assertNotIn("draw_pile", sent_state["player"])
        self.assertNotIn("tactical_order", sent_state)
        self.assertNotIn("combat_policy", sent_state)
        self.assertNotIn("unrelated_secret", json.dumps(request, ensure_ascii=False))
        self.assertIn('"player_hp_loss_including_self_cost_uncapped":4', request["questions"]["action"]["criteria"]["attack-1"])
        self.assertIn('"pareto_tradeoff_lines"', request["questions"]["action"]["criteria"]["attack-1"])
        instructions = request["questions"]["action"]["instructions"]
        self.assertIn("1:1", instructions)
        self.assertIn("Pareto", instructions)
        self.assertNotIn("prefer Attack", instructions)
        self.assertIn("X", instructions)
        self.assertIn("HP", instructions)
        self.assertIn("不根据角色、进阶或‘偏攻’口号机械加权", instructions)
        self.assertIn("usable/critical 只是本回合风险注释", instructions)
        self.assertIn("格挡能挡下明显的本回合伤害", instructions)
        self.assertIn("攻击只造成少量非致死伤害，通常优先保留防守线", instructions)
        self.assertIn("完整且同能量线路仍打不到敌方HP，就由本地过滤器移除", instructions)
        self.assertNotIn("低进阶铁甲战士", instructions)

    def test_strength_power_request_requires_measured_current_payoff_and_marks_future_uncertain(self):
        state = {
            "game_version": "v0.107.1",
            "state_type": "monster",
            "run": {"act": 1, "floor": 5, "ascension": 0},
            "player": {
                "character": "Ironclad", "hp": 40, "max_hp": 80, "block": 0, "energy": 2,
                "hand": [
                    {"id": "INFLAME", "name": "燃烧", "type": "Power", "cost": "1",
                     "description": "获得2点力量。", "index": 0},
                    {"id": "STRIKE_IRONCLAD", "name": "打击", "type": "Attack", "cost": "1",
                     "description": "造成6点伤害。", "index": 1},
                ],
                "draw_pile": [
                    {"id": "STRIKE", "type": "Attack", "description": "造成6点伤害。"},
                    {"id": "POMMEL_STRIKE", "type": "Attack", "description": "造成9点伤害。"},
                ],
                "discard_pile": [],
                "exhaust_pile": [],
            },
            "battle": {"enemies": [{
                "entity_id": "enemy-1", "hp": 50, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:enemy-1", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "enemy-1"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        actions, policy = combat_candidate_policy(state, actions)
        self.assertIn("card:0", [action.action_id for action in actions])
        self.assertFalse(policy["combat_strategy"]["effective_damage_is_a_turn_action_default"])
        state["combat_policy"] = policy

        request = build_systemone_request(state=state, actions=actions, model="kev-latest")
        line = next(item for item in policy["turn_line_candidates"] if item["first_action_id"] == "card:0")
        criteria = request["questions"]["action"]["criteria"][line["line_id"]]
        outcome = json.loads(criteria.split(" | ", 1)[1])
        payoff = outcome["first_action_context"]["setup_payoff_evidence"]
        self.assertEqual(payoff["card_type"], "Power")
        self.assertEqual(payoff["effects"], ["strength"])
        self.assertTrue(payoff["current_turn_attack_payoff_verified"])
        self.assertEqual(payoff["current_turn_hp_damage_without_setup"], 6)
        self.assertEqual(payoff["current_turn_hp_damage_with_setup"], 8)
        self.assertEqual(payoff["current_turn_incremental_hp_damage"], 2)
        self.assertEqual(payoff["energy_spent_by_setup"], 1.0)
        self.assertNotIn("visible_future_strength_potential", payoff)
        instructions = request["questions"]["action"]["instructions"]
        self.assertIn("未来抽到的攻击不能当成当前伤害", instructions)
        self.assertIn("完整线路实际穿过敌人格挡并增加HP伤害", instructions)
        self.assertIn("本地过滤", instructions)
        self.assertIn("不能凭牌堆里看得到攻击", instructions)
        self.assertNotIn("低进阶铁甲战士", instructions)
        self.assertIn("力量设置、攻击与格挡的完整线路", instructions)


class KevWholeLineChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_choice_returns_the_selected_whole_line_and_legal_first_action(self):
        actions = [
            GameAction("attack", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("defend", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        attack_step = {"kind": "play_card", "card_id": "STRIKE", "card_name": "Strike", "card_type": "Attack", "is_upgraded": False, "target_id": "E"}
        state = {
            "state_type": "monster",
            "game_version": "v0.107.1",
            "player": {"hp": 20, "block": 0, "energy": 2, "hand": [], "potions": []},
            "battle": {"enemies": [{"entity_id": "E", "hp": 10, "block": 0, "intents": []}]},
            "combat_policy": {"turn_line_candidates": [
                {"line_id": "turn-line:0", "first_action_id": "attack", "step_action_ids": ["attack", "next-attack"],
                 "steps": [attack_step, {**attack_step, "card_id": "STRIKE_B", "card_name": "Strike B"}],
                 "basis": "supported_complete_current_turn_line", "outcome": {"enemy_hp_damage": 10, "total_hp_loss_including_self_cost_before_hp_cap": 0}},
                {"line_id": "turn-line:1", "first_action_id": "defend", "step_action_ids": ["defend"],
                 "steps": [{"kind": "play_card", "card_id": "DEFEND", "card_name": "Defend", "card_type": "Skill", "target_id": None}],
                 "basis": "supported_complete_current_turn_line", "outcome": {"enemy_hp_damage": 0, "total_hp_loss_including_self_cost_before_hp_cap": 0}},
                {"line_id": "turn-line:2", "first_action_id": "end-turn", "step_action_ids": ["end-turn"],
                 "steps": [{"kind": "end_turn"}], "basis": "supported_complete_current_turn_line",
                 "outcome": {"enemy_hp_damage": 0, "total_hp_loss_including_self_cost_before_hp_cap": 0}},
            ]},
        }

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"answers": {"action": {
                    "choice": "turn-line:0",
                    "probabilities": {"turn-line:0": 0.6, "turn-line:1": 0.3, "turn-line:2": 0.1},
                    "confidence": 0.7,
                }}}

        class FakeClient:
            request = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, *, json):
                self.request = json
                return FakeResponse()

        client = FakeClient()
        with patch("sts2_agent.providers.kev.httpx.AsyncClient", return_value=client):
            decision = await KevStructuredProvider("http://127.0.0.1:8009", "kev-latest", 3.0).choose(
                state=state, actions=actions, min_confidence=0.0,
            )

        self.assertEqual(set(client.request["questions"]["action"]["criteria"]), {"turn-line:0", "turn-line:1", "turn-line:2"})
        self.assertEqual(decision.action_id, "attack")
        self.assertEqual(decision.line_choice_id, "turn-line:0")
        self.assertEqual(len(decision.plan_steps), 2)
        self.assertEqual(decision.probabilities, {"attack": 0.6, "defend": 0.3, "end-turn": 0.1})


if __name__ == "__main__":
    unittest.main()
