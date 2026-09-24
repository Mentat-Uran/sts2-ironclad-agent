from __future__ import annotations

import unittest

from sts2_agent.combat_tactics import (
    _action_block_details,
    _turn_line_choice_candidates,
    combat_candidate_policy,
    combat_pressure,
    emergency_defense_override,
)
from sts2_agent.models import GameAction


def lethal_vantom_state():
    return {
        "state_type": "boss",
        "player": {
            "hp": 20, "max_hp": 87, "block": 0, "energy": 3,
            "hand": [
                {"id": "DEFEND_IRONCLAD", "type": "Skill", "cost": "1", "description": "获得5点格挡", "index": 0},
                {"id": "SHRUG_IT_OFF", "type": "Skill", "cost": "1", "description": "获得8点格挡。抽1张牌。", "index": 1},
                {"id": "DEFEND_IRONCLAD", "type": "Skill", "cost": "1", "description": "获得5点格挡", "index": 2},
                {"id": "CINDER", "type": "Attack", "cost": "2", "description": "造成23点伤害。", "index": 3},
            ],
        },
        "battle": {
            "enemies": [{
                "entity_id": "VANTOM_0", "hp": 93, "block": 0,
                "intents": [{"type": "Attack", "label": "28"}, {"type": "StatusCard", "label": "3"}],
            }],
        },
    }


class CombatPressureTests(unittest.TestCase):
    def test_pressure_marks_twenty_hp_against_twenty_eight_damage_lethal(self):
        pressure = combat_pressure(lethal_vantom_state())
        self.assertIsNotNone(pressure)
        self.assertEqual(pressure["incoming_attack_damage"], 28)
        self.assertEqual(pressure["projected_hp_loss_if_turn_ended_now"], 28)
        self.assertTrue(pressure["lethal_if_turn_ended_now"])

    def test_multihit_and_current_block_are_counted(self):
        state = lethal_vantom_state()
        state["player"]["hp"] = 20
        state["player"]["block"] = 9
        state["battle"]["enemies"][0]["intents"] = [{"type": "Attack", "label": "8×2"}]
        pressure = combat_pressure(state)
        self.assertEqual(pressure["incoming_attack_damage"], 16)
        self.assertEqual(pressure["projected_hp_loss_if_turn_ended_now"], 7)
        self.assertFalse(pressure["lethal_if_turn_ended_now"])


class CombatOutcomeRegressionTests(unittest.TestCase):
    def test_resolved_block_text_is_not_modified_by_frail_twice(self):
        state = {
            "player": {
                "hp": 20, "max_hp": 80, "block": 0, "energy": 1,
                "status": [{"id": "FRAIL_POWER", "name": "Frail", "amount": 1}],
                "hand": [{
                    "id": "DEFEND_IRONCLAD", "name": "Defend", "type": "Skill",
                    "cost": "1", "description": "Gain 3 Block.", "index": 0,
                }],
            },
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 20, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        action = GameAction("card:0", "play_card", "combat", "Play Defend", {"card_index": 0})

        details = _action_block_details(state, action)

        self.assertEqual(details["amount"], 3)
        self.assertEqual(details["source"], "live_resolved_card_text")
        self.assertFalse(details["uncertain"])

    def test_base_and_resolved_block_text_use_one_modifier_pass(self):
        state = {
            "player": {
                "hp": 20, "max_hp": 80, "block": 0, "energy": 1,
                "status": [{"id": "FRAIL_POWER", "name": "Frail", "amount": 1}],
                "hand": [{
                    "id": "DEFEND_IRONCLAD", "name": "Defend", "type": "Skill",
                    "cost": "1", "base_description": "Gain 5 Block.",
                    "resolved_rules_text": "Gain 3 Block.",
                    "description": "Gain 3 Block.", "index": 0,
                }],
            },
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 20, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        action = GameAction("card:0", "play_card", "combat", "Play Defend", {"card_index": 0})

        details = _action_block_details(state, action)

        self.assertEqual(details["amount"], 3)
        self.assertEqual(details["source"], "base_description")
        self.assertFalse(details["uncertain"])
        projected = _action_block_details(state, action, additional_dexterity=2)
        self.assertEqual(projected["amount"], 5)
        self.assertFalse(projected["uncertain"])

    def test_frail_live_text_dexterity_projection_is_conservative_and_marked(self):
        state = {
            "player": {
                "status": [{"id": "FRAIL_POWER", "name": "Frail", "amount": 1}],
                "hand": [{"id": "DEFEND_IRONCLAD", "type": "Skill", "cost": "1",
                          "description": "Gain 3 Block.", "index": 0}],
            },
        }
        action = GameAction("card:0", "play_card", "combat", "Play Defend", {"card_index": 0})

        projected = _action_block_details(state, action, additional_dexterity=2)

        self.assertEqual(projected["amount"], 4)
        self.assertTrue(projected["uncertain"])

    def test_survival_line_keeps_uncapped_loss_when_every_line_is_lethal(self):
        state = {
            "player": {"hp": 3, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "DEFEND_IRONCLAD", "name": "Defend", "type": "Skill", "cost": "1",
                 "description": "Gain 5 Block.", "index": 0},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "10"}],
            }]},
        }
        defend = GameAction("card:0", "play_card", "combat", "Play Defend", {"card_index": 0})
        end_turn = GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"})

        _candidates, policy = combat_candidate_policy(state, [defend, end_turn])
        assessments = {item["action_id"]: item for item in policy["action_assessments"]}
        defend_line = assessments["card:0"]["joint_turn_lines"]["best_player_hp_preservation_line"]
        end_line = assessments["end-turn"]["joint_turn_lines"]["best_player_hp_preservation_line"]

        self.assertFalse(defend_line["survives_this_turn_estimate"])
        self.assertFalse(end_line["survives_this_turn_estimate"])
        self.assertEqual(defend_line["projected_hp_loss_if_turn_ended_estimate"], 3)
        self.assertEqual(end_line["projected_hp_loss_if_turn_ended_estimate"], 3)
        self.assertEqual(defend_line["incoming_hp_loss_before_player_hp_cap_estimate"], 5)
        self.assertEqual(end_line["incoming_hp_loss_before_player_hp_cap_estimate"], 10)
        self.assertEqual(defend_line["projected_total_hp_loss_uncapped_estimate"], 5)
        self.assertEqual(end_line["projected_total_hp_loss_uncapped_estimate"], 10)
        summary = next(
            row for row in policy["turn_outcome_comparison"]["candidates"]
            if row["first_action_id"] == "card:0"
        )
        self.assertEqual(summary["survival_line"]["player_hp_loss"], 3)
        self.assertEqual(summary["survival_line"]["player_hp_loss_before_hp_cap"], 5)

    def test_lethal_attack_with_a_remaining_block_action_is_not_preempted(self):
        state = lethal_vantom_state()
        state["player"]["hp"] = 28
        state["player"]["energy"] = 3
        state["player"]["hand"][3]["cost"] = "1"
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
            GameAction("card:1", "play_card", "combat", "Play Shrug It Off; 获得8点格挡。抽1张牌。", {"action": "play_card", "card_index": 1}),
            GameAction("card:2", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 2}),
            GameAction("card:3:target:VANTOM_0", "play_card", "combat", "Play Cinder on Vantom; 造成23点伤害。", {"action": "play_card", "card_index": 3, "target": "VANTOM_0"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        override = emergency_defense_override(state, actions, actions[3])
        self.assertIsNone(override)

    def test_nonlethal_positive_damage_trade_is_left_to_kev_even_when_block_exists(self):
        state = lethal_vantom_state()
        state["player"]["hp"] = 29
        state["player"]["energy"] = 1
        state["player"]["hand"][3]["cost"] = "1"
        state["player"]["hand"][3]["description"] = "造成6点伤害。"
        state["battle"]["enemies"][0]["hp"] = 8
        state["battle"]["enemies"][0]["intents"] = [{"type": "Attack", "label": "14"}]
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
            GameAction("card:3:target:VANTOM_0", "play_card", "combat", "Play Cinder; 造成6点伤害", {"action": "play_card", "card_index": 3, "target": "VANTOM_0"}),
        ]
        override = emergency_defense_override(state, actions, actions[1])
        self.assertIsNone(override)

    def test_guard_preserves_more_hp_even_when_all_known_lines_remain_lethal(self):
        state = lethal_vantom_state()
        state["player"]["hp"] = 20
        state["player"]["energy"] = 1
        state["player"]["hand"][1]["description"] = "获得8点格挡。"
        state["player"]["hand"][1]["type"] = "Skill"
        state["player"]["hand"][3]["cost"] = "1"
        actions = [
            GameAction("card:1", "play_card", "combat", "Play Block; 获得8点格挡", {"action": "play_card", "card_index": 1}),
            GameAction("card:3:target:VANTOM_0", "play_card", "combat", "Play Cinder", {"action": "play_card", "card_index": 3, "target": "VANTOM_0"}),
        ]
        override = emergency_defense_override(state, actions, actions[1])
        self.assertIsNotNone(override)
        replacement, reason = override
        self.assertEqual(replacement.action_id, "card:1")
        self.assertEqual(reason["guard_reason"], "all_modeled_lines_lethal_minimize_uncapped_hp_loss")
        self.assertLess(
            reason["replacement_projected_total_hp_loss"],
            reason["rejected_projected_total_hp_loss_uncapped"],
        )

    def test_lethal_attack_line_is_replaced_only_when_a_supported_defense_line_survives(self):
        state = nibbit_state()
        state["player"]["hp"] = 8
        state["player"]["energy"] = 1
        state["battle"]["enemies"][0]["hp"] = 30
        state["battle"]["enemies"][0]["intents"] = [{"type": "Attack", "label": "12"}]
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:NIBBIT_0", "play_card", "combat", "Play Strike; 造成6点伤害。", {"action": "play_card", "card_index": 1, "target": "NIBBIT_0"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        override = emergency_defense_override(state, actions, actions[1])
        self.assertIsNotNone(override)
        replacement, reason = override
        self.assertEqual(replacement.action_id, "card:0")
        self.assertEqual(reason["guard_reason"], "lethal_turn_supported_line_preserves_survival")
        self.assertEqual(reason["replacement_line_action_ids"], ["card:0"])

    def test_lethal_guard_does_not_replace_attack_when_same_turn_line_survives(self):
        state = nibbit_state()
        state["player"]["hp"] = 8
        state["player"]["energy"] = 2
        state["battle"]["enemies"][0]["hp"] = 30
        state["battle"]["enemies"][0]["intents"] = [{"type": "Attack", "label": "12"}]
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:NIBBIT_0", "play_card", "combat", "Play Strike; 造成6点伤害。", {"action": "play_card", "card_index": 1, "target": "NIBBIT_0"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        self.assertIsNone(emergency_defense_override(state, actions, actions[1]))

    def test_nonlethal_guard_does_not_replace_a_play_that_leaves_energy_for_block(self):
        state = lethal_vantom_state()
        state["player"]["hp"] = 29
        state["player"]["energy"] = 2
        state["player"]["hand"][3]["cost"] = "1"
        state["player"]["hand"][3]["description"] = "造成6点伤害。"
        state["battle"]["enemies"][0]["hp"] = 8
        state["battle"]["enemies"][0]["intents"] = [{"type": "Attack", "label": "14"}]
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
            GameAction("card:3:target:VANTOM_0", "play_card", "combat", "Play Cinder; 造成6点伤害", {"action": "play_card", "card_index": 3, "target": "VANTOM_0"}),
        ]
        self.assertIsNone(emergency_defense_override(state, actions, actions[1]))

    def test_unknown_attack_label_does_not_trigger_guard(self):
        state = lethal_vantom_state()
        state["battle"]["enemies"][0]["intents"][0]["label"] = "?"
        self.assertIsNone(combat_pressure(state))

    def test_nonlethal_end_turn_is_not_replaced_by_small_block_when_no_exact_kill_exists(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["hp"] = 20
        state["player"]["hand"] = state["player"]["hand"][:2]
        actions = nibbit_actions()[:2] + [nibbit_actions()[-1]]

        self.assertIsNone(emergency_defense_override(state, actions, actions[-1]))

    def test_exact_attacker_kill_replaces_end_turn_when_it_beats_best_block(self):
        state = nibbit_state()
        actions = nibbit_actions()

        override = emergency_defense_override(state, actions, actions[-1])

        self.assertIsNotNone(override)
        replacement, reason = override
        self.assertEqual(replacement.action_id, "card:1:target:NIBBIT_0")
        self.assertEqual(reason["guard_reason"], "verified_attacker_kill_no_worse_than_best_block")
        self.assertEqual(reason["replacement_projected_total_hp_loss"], 0)
        self.assertEqual(reason["best_block_projected_hp_loss"], 3)


def nibbit_state():
    return {
        "state_type": "monster",
        "run": {"act": 1, "floor": 3, "ascension": 0},
        "player": {
            "hp": 72,
            "max_hp": 80,
            "block": 0,
            "energy": 3,
            "status": [],
            "relics": [{"id": "BURNING_BLOOD", "name": "Burning Blood", "description": "At the end of combat, heal 6 HP."}],
            "hand": [
                {"id": "DEFEND_IRONCLAD", "type": "Skill", "cost": "1", "description": "获得5点格挡", "target_type": "Self", "is_upgraded": False, "index": 0, "can_play": True},
                {"id": "STRIKE_R", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "index": 1, "can_play": True},
                {"id": "STRIKE_R", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "target_type": "AnyEnemy", "is_upgraded": False, "index": 2, "can_play": True},
            ],
        },
        "battle": {"enemies": [{
            "entity_id": "NIBBIT_0", "name": "Nibbit", "hp": 6, "block": 0,
            "status": [], "intents": [{"type": "Attack", "label": "8"}],
        }]},
    }


def nibbit_actions():
    return [
        GameAction("card:0", "play_card", "combat", "Play Defend; 获得5点格挡", {"action": "play_card", "card_index": 0}),
        GameAction("card:1:target:NIBBIT_0", "play_card", "combat", "Play Strike on Nibbit; 造成6点伤害。", {"action": "play_card", "card_index": 1, "target": "NIBBIT_0"}),
        GameAction("card:2:target:NIBBIT_0", "play_card", "combat", "Play Strike on Nibbit; 造成6点伤害。", {"action": "play_card", "card_index": 2, "target": "NIBBIT_0"}),
        GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
    ]


class CombatCandidatePolicyTests(unittest.TestCase):
    def test_exact_attacker_kill_removes_block_line_with_same_outcome_and_extra_cost(self):
        candidates, policy = combat_candidate_policy(nibbit_state(), nibbit_actions())
        ids = [action.action_id for action in candidates]
        self.assertEqual(policy["mode"], "state_aware_tactical_comparison")
        self.assertNotIn("card:0", ids)
        self.assertIn("card:1:target:NIBBIT_0", ids)
        self.assertIn("end-turn", ids)
        self.assertIn("card:2:target:NIBBIT_0", policy["collapsed_duplicate_action_ids"])
        proof = policy["suppressed_dominated_action_proofs"]["card:0"]
        self.assertEqual(proof["dominator_action_id"], "card:1:target:NIBBIT_0")
        self.assertEqual(proof["basis"], "one_complete_supported_line_matches_tactical_outcomes_with_fewer_resources")
        self.assertLess(proof["dominator_energy_spent"], proof["dominated_energy_spent"])
        kill = next(item for item in policy["action_assessments"] if item["action_id"] == "card:1:target:NIBBIT_0")
        self.assertEqual(kill["current_attack_damage_canceled_estimate"], 8)
        self.assertEqual(kill["enemy_hp_damage_estimate"], 6)

    def test_low_ascension_never_removes_block_because_of_burning_blood(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["hp"] = 38
        state["battle"]["enemies"][0]["intents"][0]["label"] = "5"
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        self.assertEqual(policy["mode"], "state_aware_tactical_comparison")
        self.assertEqual(policy["suppressed_low_value_block_action_ids"], [])
        self.assertTrue(any(action.action_id == "card:0" for action in candidates))
        self.assertTrue(any(action.action == "play_card" for action in candidates))
        self.assertIn("end-turn", [action.action_id for action in candidates])

    def test_pure_block_is_suppressed_when_it_prevents_no_visible_hp_loss(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["intents"] = [{"type": "Debuff", "label": ""}]
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        ids = [action.action_id for action in candidates]
        self.assertNotIn("card:0", ids)
        self.assertIn("card:1:target:NIBBIT_0", ids)
        self.assertIn("end-turn", ids)
        self.assertEqual(policy["suppressed_low_value_block_action_ids"], ["card:0"])
        self.assertEqual(
            policy["suppressed_low_value_block_reasons"]["card:0"],
            "pure_block_prevents_no_visible_hp_loss_and_has_no_supported_conversion_or_retention",
        )

    def test_bloodletting_that_would_kill_player_is_removed_and_logged(self):
        state = {
            "run": {"ascension": 0},
            "player": {"hp": 1, "max_hp": 80, "block": 0, "energy": 0, "status": [], "hand": [
                {"id": "BLOODLETTING", "name": "放血", "type": "Skill", "cost": "0",
                 "description": "失去3点生命。获得2点能量。", "index": 0},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 10, "block": 0,
                                      "intents": [{"type": "Attack", "label": "6"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Bloodletting", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertEqual([item.action_id for item in candidates], ["end-turn"])
        self.assertEqual(policy["suppressed_lethal_self_cost_action_ids"], ["card:0"])
        self.assertEqual(
            policy["suppressed_lethal_self_cost_reasons"]["card:0"],
            "known_immediate_self_hp_cost_would_reduce_player_hp_to_zero",
        )
        card_assessment = next(
            item for item in policy["action_assessments"]
            if item["action_id"] == "end-turn"
        )
        self.assertNotEqual(card_assessment.get("self_hp_cost_is_lethal"), True)

    def test_nonlethal_bloodletting_reports_its_exact_live_hp_cost(self):
        state = {
            "run": {"ascension": 0},
            "player": {"hp": 5, "max_hp": 80, "block": 0, "energy": 0, "status": [], "hand": [
                {"id": "BLOODLETTING", "name": "放血", "type": "Skill", "cost": "0",
                 "description": "失去3点生命。获得2点能量。", "index": 0},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 10, "block": 0,
                                      "intents": [{"type": "Debuff", "label": ""}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Bloodletting", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in [
            *policy["action_assessments"],
            *policy.get("suppressed_zero_current_impact_assessments", []),
            *policy.get("suppressed_dominated_action_assessments", []),
        ] if item["action_id"] == "card:0")
        self.assertEqual(assessment["immediate_self_hp_cost"], 3)
        self.assertEqual(assessment["player_hp_after_self_cost_estimate"], 2)
        self.assertFalse(assessment["self_hp_cost_is_lethal"])

    def test_action_assessments_compare_actual_damage_and_block_value(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["hp"] = 38
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        by_id = {item["action_id"]: item for item in policy["action_assessments"]}
        strike = by_id["card:1:target:NIBBIT_0"]
        self.assertEqual(strike["enemy_hp_damage_estimate"], 6)
        self.assertFalse(strike["kills_target_estimate"])
        block = by_id["card:0"]
        self.assertEqual(block["hp_loss_prevented_if_turn_ended_now"], 5)

    def test_joint_line_audit_preserves_the_full_followup_action_sequence(self):
        state = nibbit_state()
        state["player"]["energy"] = 3
        state["battle"]["enemies"][0]["hp"] = 30
        state["player"]["hand"].append({
            "id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
            "description": "造成6点伤害。", "target_type": "AnyEnemy", "index": 3, "can_play": True,
        })
        actions = [
            GameAction("card:1:target:NIBBIT_0", "play_card", "combat", "Play first Strike", {"action": "play_card", "card_index": 1, "target": "NIBBIT_0"}),
            GameAction("card:2:target:NIBBIT_0", "play_card", "combat", "Play second Strike", {"action": "play_card", "card_index": 2, "target": "NIBBIT_0"}),
            GameAction("card:3:target:NIBBIT_0", "play_card", "combat", "Play third Strike", {"action": "play_card", "card_index": 3, "target": "NIBBIT_0"}),
            GameAction("card:0", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        first = next(item for item in policy["action_assessments"] if item["action_id"] == actions[0].action_id)
        line = first["joint_turn_lines"]["best_enemy_hp_damage_line"]
        self.assertEqual(line["line_action_ids"], [
            "card:1:target:NIBBIT_0", "card:2:target:NIBBIT_0", "card:3:target:NIBBIT_0",
        ])
        self.assertEqual(line["enemy_hp_damage_estimate"], 18)

    def test_enemy_block_absorbs_damage_before_hp_and_is_not_mistaken_for_a_kill(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["block"] = 6
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        strike = next(item for item in policy["action_assessments"] if item["action_id"] == "card:1:target:NIBBIT_0")
        self.assertEqual(strike["enemy_block_removed_estimate"], 6)
        self.assertEqual(strike["enemy_hp_damage_estimate"], 0)
        self.assertEqual(strike["immediate_attack_result"], "enemy_block_only")
        self.assertFalse(strike["kills_target_estimate"])
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        self.assertEqual(
            policy["suppressed_dominated_action_proofs"]["card:0"]["dominator_action_id"],
            "card:1:target:NIBBIT_0",
        )

    def test_temporary_strength_is_not_a_payoff_when_it_adds_no_enemy_hp_damage(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "临时力量（测试动作）", "type": "Skill", "cost": "1",
                 "description": "获得2点力量。本回合。", "index": 0},
                {"id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 8,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        assessment = next(
            item for item in policy["suppressed_zero_current_impact_assessments"]
            if item["action_id"] == "card:0"
        )
        self.assertTrue(assessment["has_affordable_attack_candidate_this_turn"])
        self.assertFalse(assessment["has_verified_attack_hp_payoff_this_turn"])
        self.assertEqual(
            policy["suppressed_zero_current_impact_reasons"]["card:0"],
            "temporary_strength_without_incremental_hp_damage",
        )

    def test_block_only_attack_does_not_spend_the_only_energy_for_effective_defense(self):
        state = {
            "player": {"hp": 29, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1",
                 "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 20, "block": 6,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        attack = next(
            item for item in policy["suppressed_no_hp_damage_attack_assessments"]
            if item["action_id"] == actions[0].action_id
        )
        self.assertEqual(attack["enemy_hp_damage_estimate"], 0)
        self.assertEqual(attack["enemy_block_removed_estimate"], 6)
        self.assertNotIn(actions[0].action_id, [action.action_id for action in candidates])
        self.assertIn("card:1", [action.action_id for action in candidates])

    def test_block_only_attack_defends_first_when_both_actions_fit_and_no_attack_converts_to_hp(self):
        state = {
            "player": {"hp": 29, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1",
                 "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 20, "block": 6,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
        ]
        override = emergency_defense_override(state, actions, actions[0])
        self.assertIsNotNone(override)
        replacement, reason = override
        self.assertEqual(replacement.action_id, "card:1")
        self.assertTrue(reason["block_and_attack_both_affordable_this_turn"])
        self.assertEqual(reason["guard_reason"], "block_only_attack_has_no_current_hp_conversion_defend_first")

    def test_enemy_block_chip_is_removed_when_defense_has_same_result_for_less_energy(self):
        state = {
            "player": {"hp": 29, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1",
                 "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 20, "block": 6,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0:target:E", [action.action_id for action in _candidates])
        by_id = {item["action_id"]: item for item in [
            *policy["action_assessments"],
            *policy["suppressed_dominated_action_assessments"],
        ]}
        attack_assessment = by_id["card:0:target:E"]
        attack_line = attack_assessment["joint_turn_lines"]["best_player_hp_preservation_line"]
        defend_line = by_id["card:1"]["joint_turn_lines"]["best_player_hp_preservation_line"]
        self.assertEqual(attack_line["enemy_hp_damage_estimate"], 0)
        self.assertEqual(defend_line["enemy_hp_damage_estimate"], 0)
        self.assertEqual(attack_line["projected_hp_loss_if_turn_ended_estimate"], 3)
        self.assertEqual(defend_line["projected_hp_loss_if_turn_ended_estimate"], 3)
        self.assertEqual(attack_line["enemy_block_removed_estimate"], 6)
        self.assertEqual(defend_line["enemy_block_removed_estimate"], 0)
        self.assertFalse(attack_line["enemy_block_removal_counted_as_tactical_rank"])
        self.assertEqual(attack_assessment["current_tactical_effect_class"], "enemy_block_chip_only")
        self.assertNotIn("card:0:target:E", policy["provider_candidate_action_ids"])
        proof = policy["suppressed_dominated_action_proofs"]["card:0:target:E"]
        self.assertEqual(proof["basis"], "one_complete_supported_line_matches_tactical_outcomes_with_fewer_resources")

    def test_plain_attack_that_only_clears_block_is_removed_without_hp_followup(self):
        state = {
            "player": {"hp": 29, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE_R", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1",
                 "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 20, "block": 6,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0:target:E", [action.action_id for action in candidates])
        self.assertIn("card:1", [action.action_id for action in candidates])
        self.assertNotIn("end-turn", [action.action_id for action in candidates])
        self.assertTrue(any(
            item["first_action_id"] == "end-turn"
            and item["basis"] == "strict_current_turn_damage_and_hp_dominance"
            for item in policy["suppressed_dominated_turn_lines"]
        ))
        self.assertEqual(
            policy["suppressed_no_hp_damage_attack_reasons"]["card:0:target:E"],
            "complete_supported_line_only_removes_enemy_block_and_deals_no_enemy_hp_damage",
        )
        self.assertEqual(
            policy["suppressed_no_hp_damage_attack_assessments"][0]["enemy_hp_damage_estimate"], 0
        )

    def test_delayed_power_block_is_not_reported_as_current_block_and_self_cost_is_visible(self):
        state = {
            "player": {"hp": 44, "max_hp": 80, "block": 5, "energy": 0, "hand": [
                {"id": "CRIMSON_MANTLE", "name": "绯红披风", "type": "Power", "cost": "0",
                 "description": "在你的回合开始时，失去1点生命并获得8点格挡。", "index": 0},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 86, "block": 0,
                                      "intents": [{"type": "Attack", "label": "4"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Crimson Mantle", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0")
        self.assertEqual(assessment["delayed_block_per_future_turn_estimate"], 8)
        self.assertEqual(assessment["self_hp_loss_per_future_turn_estimate"], 1)
        self.assertEqual(assessment["visible_hp_loss_if_ending_now"], 0)
        self.assertNotIn("block_added_from_live_text", assessment)
        self.assertNotIn("hp_loss_prevented_if_turn_ended_now", assessment)

    def test_unmodeled_intent_does_not_remove_any_legal_candidate(self):
        state = nibbit_state()
        state["battle"]["enemies"][0]["hp"] = 38
        state["battle"]["enemies"][0]["intents"].append({"type": "Debuff", "label": "", "description": "Apply Weak."})
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        self.assertIn("card:0", [action.action_id for action in candidates])
        self.assertEqual(policy["suppressed_low_value_block_action_ids"], [])

    def test_multi_attacker_kill_does_not_hide_defense_or_remaining_threat(self):
        state = nibbit_state()
        state["battle"]["enemies"].append({
            "entity_id": "OTHER_0", "name": "Other", "hp": 50, "block": 0,
            "status": [], "intents": [{"type": "Attack", "label": "12"}],
        })
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        self.assertEqual(policy["mode"], "state_aware_tactical_comparison")
        self.assertIn("card:0", [action.action_id for action in candidates])
        self.assertIn("card:1:target:NIBBIT_0", [action.action_id for action in candidates])
        self.assertIn("card:1:target:NIBBIT_0", policy["guaranteed_lethal_action_ids"])

    def test_not_low_ascension_keeps_balanced_candidate_set(self):
        state = nibbit_state()
        state["run"]["ascension"] = 2
        state["battle"]["enemies"][0]["hp"] = 38
        candidates, policy = combat_candidate_policy(state, nibbit_actions())
        self.assertEqual(policy["mode"], "state_aware_tactical_comparison")
        self.assertIn("card:0", [action.action_id for action in candidates])

    def test_tremble_without_an_affordable_attack_is_removed_and_defend_remains(self):
        state = {
            "state_type": "monster",
            "run": {"act": 1, "floor": 8, "ascension": 0},
            "player": {
                "hp": 37, "max_hp": 80, "block": 0, "energy": 1,
                "hand": [
                    {"id": "TREMBLE", "name": "战栗", "type": "Skill", "cost": "1", "description": "给予3层易伤。 消耗。", "index": 0},
                    {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1", "description": "获得3点格挡。", "index": 1},
                    {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1", "description": "获得3点格挡。", "index": 2},
                ],
            },
            "battle": {"enemies": [
                {"entity_id": "TRACKER_0", "name": "劫掠者追踪手", "hp": 4, "block": 0, "status": [], "intents": [{"type": "Attack", "label": "1×8"}]},
                {"entity_id": "CROSSBOW_0", "name": "劫掠者弩手", "hp": 19, "block": 0, "status": [], "intents": [{"type": "Defend", "label": ""}]},
            ]},
        }
        actions = [
            GameAction("card:0:target:TRACKER_0", "play_card", "combat", "Play Tremble; 给予3层易伤。 消耗。", {"action": "play_card", "card_index": 0, "target": "TRACKER_0"}),
            GameAction("card:1", "play_card", "combat", "Play Defend; 获得3点格挡。", {"action": "play_card", "card_index": 1}),
            GameAction("card:2", "play_card", "combat", "Play Defend; 获得3点格挡。", {"action": "play_card", "card_index": 2}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        tremble_id = "card:0:target:TRACKER_0"
        self.assertNotIn(tremble_id, [action.action_id for action in candidates])
        self.assertIn("card:1", [action.action_id for action in candidates])
        assessment = next(
            item for item in policy["suppressed_zero_current_impact_assessments"]
            if item["action_id"] == tremble_id
        )
        self.assertEqual(assessment["setup_effect_kinds"], ["vulnerable"])
        self.assertFalse(assessment["has_affordable_attack_candidate_this_turn"])
        self.assertEqual(
            policy["suppressed_zero_current_impact_reasons"][tremble_id],
            "vulnerable_without_incremental_current_turn_hp_damage",
        )
        self.assertEqual(assessment["visible_hp_loss_if_ending_now"], 8)
        self.assertEqual(assessment["current_turn_continuation"]["best_followup_fixed_attack_hp_damage_estimate"], 0)
        self.assertTrue(assessment["joint_turn_lines"]["supported"])
        self.assertEqual(assessment["joint_turn_lines"]["best_enemy_hp_damage_line"]["enemy_hp_damage_estimate"], 0)

    def test_tremble_is_kept_only_when_same_turn_attack_converts_vulnerable_to_hp_damage(self):
        state = {
            "state_type": "monster",
            "run": {"act": 1, "floor": 4, "ascension": 0},
            "player": {"hp": 70, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "TREMBLE", "name": "战栗", "type": "Skill", "cost": "1",
                 "description": "给予3层易伤。 消耗。", "index": 0},
                {"id": "STRIKE_IRONCLAD", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成8点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 40, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "4"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Tremble", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertIn("card:0:target:E", [action.action_id for action in candidates])
        tremble = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        comparison = tremble["attack_sequence_comparison"]
        self.assertEqual(comparison["best_hp_damage_without_setup"], 8)
        self.assertEqual(comparison["best_hp_damage_after_setup"], 12)
        self.assertEqual(comparison["incremental_hp_damage_after_setup"], 4)
        self.assertTrue(tremble["has_verified_attack_hp_payoff_this_turn"])
        line = tremble["joint_turn_lines"]["best_enemy_hp_damage_line"]
        self.assertEqual(line["enemy_hp_damage_estimate"], 12)
        self.assertEqual(line["line_action_ids"][:2], ["card:0:target:E", "card:1:target:E"])

    def test_attack_estimate_applies_a_live_vulnerable_status(self):
        state = {
            "player": {"hp": 50, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE_IRONCLAD", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成8点伤害。", "index": 0},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 40, "block": 0,
                "status": [{"id": "VULNERABLE_POWER", "name": "易伤", "amount": 2}],
                "intents": [{"type": "Attack", "label": "3"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End the player turn.", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        strike = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        self.assertIn("card:0:target:E", [action.action_id for action in candidates])
        self.assertEqual(strike["enemy_hp_damage_estimate"], 12)
        self.assertTrue(strike["attack_damage_estimate_is_exact"])

    def test_rampage_growth_text_keeps_current_damage_and_attacker_kill_estimate(self):
        state = {
            "player": {"hp": 13, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "RAMPAGE", "name": "暴走", "type": "Attack", "cost": "0",
                 "description": "造成16点伤害。将这张牌在本场战斗中的伤害增加5。", "index": 0},
                {"id": "TRUE_GRIT", "name": "坚毅", "type": "Skill", "cost": "1",
                 "description": "获得7点格挡。随机消耗一张牌。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "CUBEX_CONSTRUCT_0", "hp": 6, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "15×2"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:CUBEX_CONSTRUCT_0", "play_card", "combat", "Play Rampage", {"action": "play_card", "card_index": 0, "target": "CUBEX_CONSTRUCT_0"}),
            GameAction("card:1", "play_card", "combat", "Play True Grit", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertEqual([action.action_id for action in candidates], [action.action_id for action in actions])
        rampage = next(item for item in policy["action_assessments"] if item["action_id"] == actions[0].action_id)
        self.assertEqual(rampage["displayed_damage"], 16)
        self.assertEqual(rampage["enemy_hp_damage_estimate"], 6)
        self.assertTrue(rampage["kills_target_estimate"])
        self.assertEqual(rampage["current_attack_damage_canceled_estimate"], 30)
        self.assertEqual(rampage["projected_hp_loss_if_turn_ended_after_action_estimate"], 0)
        end_turn = next(item for item in policy["action_assessments"] if item["action_id"] == "end-turn")
        self.assertTrue(end_turn["lethal_if_turn_ended_now"])
        self.assertTrue(end_turn["lethal_if_turn_ended_after_action_estimate"])

        state["player"]["hand"][0]["description"] = "Deal 16 damage. Increase this card's damage by 5 this combat."
        _english_candidates, english_policy = combat_candidate_policy(state, actions)
        english_rampage = next(
            item for item in english_policy["action_assessments"] if item["action_id"] == actions[0].action_id
        )
        self.assertEqual(english_rampage["displayed_damage"], 16)

    def test_lethal_end_turn_is_replaced_with_a_verified_attack_kill(self):
        state = {
            "player": {"hp": 13, "max_hp": 80, "block": 0, "energy": 0, "status": [], "hand": [
                {"id": "RAMPAGE", "name": "暴走", "type": "Attack", "cost": "0",
                 "description": "造成16点伤害。将这张牌在本场战斗中的伤害增加5。", "index": 0},
            ]},
            "battle": {"enemies": [{
                "entity_id": "CUBEX_CONSTRUCT_0", "hp": 6, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "15×2"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:CUBEX_CONSTRUCT_0", "play_card", "combat", "Play Rampage", {"action": "play_card", "card_index": 0, "target": "CUBEX_CONSTRUCT_0"}),
            GameAction("potion:0", "use_potion", "combat", "Use Thorns Potion", {"action": "use_potion", "potion_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        override = emergency_defense_override(state, actions, actions[-1])
        self.assertIsNotNone(override)
        replacement, pressure = override
        self.assertEqual(replacement.action_id, actions[0].action_id)
        self.assertEqual(pressure["guard_reason"], "verified_attacker_kill_prevents_lethal_end_turn")
        self.assertEqual(pressure["replacement_projected_hp_loss_if_turn_ended"], 0)

    def test_exact_attacker_kill_dominates_a_pure_block_with_worse_hp_outcome(self):
        state = nibbit_state()
        actions = nibbit_actions()
        override = emergency_defense_override(state, actions, actions[0])
        self.assertIsNotNone(override)
        replacement, pressure = override
        self.assertEqual(replacement.action_id, "card:1:target:NIBBIT_0")
        self.assertEqual(pressure["guard_reason"], "verified_kill_dominates_pure_block")

    def test_composite_block_effect_is_left_for_kev_to_compare(self):
        state = nibbit_state()
        state["player"]["hand"][0]["description"] = "获得5点格挡。抽1张牌。"
        actions = nibbit_actions()
        actions[0] = GameAction("card:0", "play_card", "combat", "Play draw Block", {"action": "play_card", "card_index": 0})
        override = emergency_defense_override(state, actions, actions[0])
        self.assertIsNone(override)

    def test_safe_block_is_not_replaced_by_an_attack(self):
        state = {
            "player": {"hp": 13, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "RAMPAGE", "name": "暴走", "type": "Attack", "cost": "0",
                 "description": "造成16点伤害。将这张牌在本场战斗中的伤害增加5。", "index": 0},
                {"id": "LARGE_BLOCK", "name": "护甲", "type": "Skill", "cost": "1",
                 "description": "获得18点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "CUBEX_CONSTRUCT_0", "hp": 6, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "15×2"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:CUBEX_CONSTRUCT_0", "play_card", "combat", "Play Rampage", {"action": "play_card", "card_index": 0, "target": "CUBEX_CONSTRUCT_0"}),
            GameAction("card:1", "play_card", "combat", "Play Block", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        self.assertIsNone(emergency_defense_override(state, actions, actions[1]))

    def test_setup_is_marked_with_attack_payoff_when_energy_can_cover_both(self):
        state = {
            "player": {"hp": 37, "max_hp": 80, "block": 0, "energy": 2, "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1", "description": "获得2点力量。 本回合。", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "name": "Enemy", "hp": 30, "block": 0, "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0")
        self.assertTrue(assessment["has_affordable_attack_candidate_this_turn"])
        self.assertEqual(assessment["affordable_attack_candidates_this_turn"][0]["name"], "打击")
        self.assertEqual(
            assessment["attack_sequence_comparison"]["incremental_hp_damage_after_setup"],
            2,
        )

    def test_temporary_strength_compares_full_affordable_attack_sequence(self):
        state = {
            "player": {"hp": 37, "max_hp": 80, "block": 0, "energy": 3, "status": [], "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1",
                 "description": "Gain 2 Strength. At the end of this turn, lose 2 Strength.", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 2},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "name": "Enemy", "hp": 30, "block": 8,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 2, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0")
        comparison = assessment["attack_sequence_comparison"]
        self.assertEqual(comparison["best_hp_damage_without_setup"], 4)
        self.assertEqual(comparison["best_hp_damage_after_setup"], 8)
        self.assertEqual(comparison["incremental_hp_damage_after_setup"], 4)
        self.assertTrue(assessment["has_verified_attack_hp_payoff_this_turn"])
        self.assertIn("card:0", [action.action_id for action in candidates])

    def test_temporary_strength_is_removed_when_its_attack_sequence_only_hits_enemy_block(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1",
                 "description": "Gain 2 Strength. At the end of this turn, lose 2 Strength.", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "name": "Enemy", "hp": 30, "block": 8,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["suppressed_zero_current_impact_assessments"] if item["action_id"] == "card:0")
        self.assertEqual(assessment["attack_sequence_comparison"]["incremental_hp_damage_after_setup"], 0)
        self.assertFalse(assessment["attack_hp_payoff_uncertain"])
        self.assertNotIn("card:0", [action.action_id for action in candidates])

    def test_temporary_strength_is_suppressed_when_x_attack_loses_damage_after_setup_cost(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 3, "status": [], "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1",
                 "description": "Gain 2 Strength. At the end of this turn, lose 2 Strength.", "index": 0},
                {"id": "WHIRLWIND", "name": "旋风斩", "type": "Attack", "cost": "X",
                 "description": "对所有敌人造成5点伤害X次。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "name": "Enemy", "hp": 30, "block": 0,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1", "play_card", "combat", "Play Whirlwind", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["suppressed_zero_current_impact_assessments"] if item["action_id"] == "card:0")
        comparison = assessment["attack_sequence_comparison"]
        self.assertEqual(comparison["best_hp_damage_without_setup"], 15)
        self.assertEqual(comparison["best_hp_damage_after_setup"], 14)
        self.assertEqual(comparison["incremental_hp_damage_after_setup"], -1)
        self.assertFalse(assessment["attack_hp_payoff_uncertain"])
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        self.assertEqual(policy["suppressed_zero_current_impact_reasons"]["card:0"], "temporary_strength_without_incremental_hp_damage")

    def test_zero_energy_whirlwind_is_not_sent_to_kev(self):
        state = {
            "player": {"hp": 30, "block": 8, "energy": 0, "status": [], "hand": [
                {"id": "WHIRLWIND", "name": "旋风斩", "type": "Attack", "cost": "X",
                 "target_type": "AllEnemies", "description": "对所有敌人造成5点伤害X次。", "index": 0},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 10, "block": 0,
                                      "status": [{"id": "VULNERABLE_POWER", "name": "易伤", "amount": 2}],
                                      "intents": [{"type": "Attack", "label": "9"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Whirlwind", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertEqual([action.action_id for action in candidates], ["end-turn"])
        attack = next(item for item in policy["suppressed_zero_current_impact_assessments"] if item["action_id"] == "card:0")
        self.assertEqual(attack["attack_hit_count_estimate"], 0)
        self.assertEqual(attack["enemy_hp_damage_estimate"], 0)
        self.assertEqual(attack["enemy_block_removed_estimate"], 0)
        self.assertTrue(attack["attack_damage_estimate_is_exact"])

    def test_whirlwind_resolves_each_hit_against_each_enemy_block_and_hp(self):
        state = {
            "player": {"hp": 30, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "WHIRLWIND", "name": "旋风斩", "type": "Attack", "cost": "X",
                 "target_type": "AllEnemies", "description": "对所有敌人造成5点伤害X次。", "index": 0},
            ]},
            "battle": {"enemies": [
                {"entity_id": "E1", "hp": 30, "block": 8, "intents": [{"type": "Attack", "label": "8"}]},
                {"entity_id": "E2", "hp": 7, "block": 0, "intents": [{"type": "Attack", "label": "4"}]},
            ]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Whirlwind", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        _candidates, policy = combat_candidate_policy(state, actions)

        attack = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0")
        self.assertEqual(attack["attack_hit_count_estimate"], 2)
        self.assertEqual(attack["enemy_hp_damage_estimate"], 9)
        self.assertEqual(attack["enemy_block_removed_estimate"], 8)
        self.assertEqual(attack["kills_target_ids_estimate"], ["E2"])
        per_target = {item["entity_id"]: item for item in attack["per_target_attack_estimates"]}
        self.assertEqual(per_target["E1"]["hp_damage"], 2)
        self.assertEqual(per_target["E1"]["block_removed"], 8)
        self.assertEqual(per_target["E2"]["hp_damage"], 7)
        self.assertEqual(per_target["E2"]["hp_after"], 0)

    def test_visible_strength_is_applied_per_hit_before_block_is_exhausted(self):
        state = {
            "player": {"hp": 30, "block": 0, "energy": 1,
                "status": [{"id": "STRENGTH_POWER", "name": "力量", "amount": 2}],
                "hand": [{"id": "TWIN_STRIKE", "name": "双重打击", "type": "Attack", "cost": "1",
                                 "description": "Deal 3 damage twice.", "index": 0}]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 20, "block": 4,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Twin Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        _candidates, policy = combat_candidate_policy(state, actions)

        attack = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        self.assertEqual(attack["attack_hit_count_estimate"], 2)
        self.assertEqual(attack["enemy_block_removed_estimate"], 4)
        self.assertEqual(attack["enemy_hp_damage_estimate"], 6)
        line = next(item for item in policy["turn_line_candidates"] if item["first_action_id"] == "card:0:target:E")
        self.assertEqual(line["first_action_context"]["attack_result"]["hit_count"], 2)
        self.assertEqual(line["first_action_context"]["attack_result"]["enemy_hp_damage"], 6)

    def test_strength_setup_without_attack_payoff_is_marked_unverified(self):
        state = {
            "player": {"hp": 37, "max_hp": 80, "block": 0, "energy": 1, "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1", "description": "获得2点力量。本回合。", "index": 0},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得3点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "name": "Enemy", "hp": 30, "block": 0, "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["suppressed_zero_current_impact_assessments"] if item["action_id"] == "card:0")
        self.assertEqual(assessment["setup_effect_kinds"], ["strength"])
        self.assertEqual(assessment["strength_gain_from_live_text"], 2)
        self.assertEqual(assessment["strength_duration"], "temporary_this_turn")
        self.assertFalse(assessment["has_affordable_attack_candidate_this_turn"])

    def test_temporary_strength_without_payoff_is_removed_but_end_turn_and_block_remain(self):
        state = {
            "player": {"hp": 30, "block": 0, "energy": 2, "hand": [
                {"id": "TEST_TEMP_STRENGTH", "name": "战力", "type": "Skill", "cost": "1", "description": "获得2点力量。本回合。", "index": 0},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得3点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 0, "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play test-only temporary Strength", {"action": "play_card", "card_index": 0}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        ids = [action.action_id for action in candidates]
        self.assertNotIn("card:0", ids)
        self.assertIn("card:1", ids)
        self.assertNotIn("end-turn", ids)
        self.assertEqual(policy["suppressed_zero_current_impact_reasons"]["card:0"], "temporary_strength_without_an_affordable_attack")

    def test_visible_current_strength_is_included_in_fixed_attack_and_kill_estimates(self):
        state = {
            "player": {"hp": 12, "max_hp": 80, "block": 0, "energy": 1,
                       "status": [{"id": "STRENGTH_POWER", "name": "力量", "amount": 2, "type": "Buff"}],
                       "hand": [{"id": "RAMPAGE", "name": "暴走", "type": "Attack", "cost": "1",
                                 "description": "造成16点伤害。", "index": 0}]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 18, "block": 0,
                                      "intents": [{"type": "Attack", "label": "14"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Rampage", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        attack = next(item for item in policy["action_assessments"] if item["action_id"] == actions[0].action_id)
        self.assertEqual(attack["displayed_damage"], 16)
        self.assertEqual(attack["visible_strength_bonus_estimate"], 2)
        self.assertEqual(attack["enemy_hp_damage_estimate"], 18)
        self.assertTrue(attack["kills_target_estimate"])
        self.assertEqual(attack["current_attack_damage_canceled_estimate"], 14)
        self.assertIn("card:0:target:E", policy["guaranteed_lethal_action_ids"])

    def test_exact_zero_damage_attack_is_removed_when_it_has_no_other_effect(self):
        state = {
            "player": {"hp": 30, "block": 0, "energy": 1, "hand": [
                {"id": "ZERO_ATTACK", "name": "空击", "type": "Attack", "cost": "1", "description": "造成0点伤害。", "index": 0},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得3点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 0, "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play 空击", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        ids = [action.action_id for action in candidates]
        self.assertNotIn("card:0:target:E", ids)
        self.assertIn("card:1", ids)
        self.assertNotIn("end-turn", ids)
        self.assertEqual(
            policy["suppressed_zero_current_impact_reasons"]["card:0:target:E"],
            "modeled_attack_has_zero_hp_damage_and_removes_no_enemy_block",
        )

    def test_block_only_hit_is_kept_when_a_free_followup_converts_it_to_hp_damage(self):
        state = {
            "player": {"hp": 29, "max_hp": 80, "block": 0, "energy": 1, "hand": [
                {"id": "STRIKE_A", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 0},
                {"id": "STRIKE_B", "name": "打击", "type": "Attack", "cost": "0", "description": "造成6点伤害。", "index": 1},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得5点格挡。", "index": 2},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 6,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike A", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike B", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 2}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        assessment = next(item for item in policy["action_assessments"] if item["action_id"] == actions[0].action_id)
        continuation = assessment["current_turn_continuation"]
        self.assertEqual(assessment["enemy_hp_damage_estimate"], 0)
        self.assertEqual(continuation["best_followup_fixed_attack_hp_damage_estimate"], 6)
        self.assertEqual(continuation["candidate_plus_attack_followup_hp_damage_upper_bound"], 6)
        self.assertIn(actions[0].action_id, [action.action_id for action in _candidates])
        self.assertIsNone(emergency_defense_override(state, actions, actions[0]))

    def test_strength_setup_and_material_block_trade_are_both_exposed_to_kev(self):
        state = {
            "player": {"hp": 37, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "INFLAME", "name": "燃烧", "type": "Power", "cost": "1", "description": "Gain 2 Strength.", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "2", "description": "造成6点伤害。", "index": 1},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得5点格挡。", "index": 2},
            ], "draw_pile": [
                {"id": "POMMEL_STRIKE", "type": "Attack", "description": "造成9点伤害。抽1张牌。"},
                {"id": "DEFEND", "type": "Skill", "description": "获得5点格挡。"},
            ], "discard_pile": [
                {"id": "IRON_WAVE", "type": "Attack", "description": "获得4点格挡。造成5点伤害。"},
            ], "exhaust_pile": [
                {"id": "EXHAUSTED_ATTACK", "type": "Attack", "description": "造成8点伤害。"},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 0,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 2}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        self.assertIn("card:2", [action.action_id for action in candidates])
        assessment = next(item for item in [
            *policy["action_assessments"],
            *policy.get("suppressed_zero_current_impact_assessments", []),
            *policy.get("suppressed_dominated_action_assessments", []),
        ] if item["action_id"] == "card:0")
        self.assertEqual(assessment["current_turn_continuation"]["remaining_energy_after_this_card"], 0)
        self.assertEqual(assessment["current_turn_continuation"]["best_followup_fixed_attack_hp_damage_estimate"], 0)
        future = assessment["future_strength_context"]
        self.assertEqual(future["draw_pile_attack_cards_visible"], 1)
        self.assertEqual(future["discard_pile_attack_cards_visible"], 1)
        self.assertEqual(future["exhaust_pile_attack_cards_excluded"], 1)
        self.assertEqual(future["visible_future_attack_cards"], 2)
        self.assertEqual(future["visible_future_attack_hit_potential"], 2)
        self.assertTrue(future["visible_attack_hits_meet_setup_support_bar"])
        self.assertEqual(future["support_status"], "visible_attack_potential")
        self.assertGreater(future["enemy_hp_after_setup_line_estimate"], 0)
        self.assertIsNone(future["future_damage_estimate"])
        self.assertTrue(future["not_guaranteed_draw_affordability_or_damage"])
        self.assertTrue(future["not_a_play_priority"])
        block_line = next(item for item in policy["turn_line_candidates"] if item["first_action_id"] == "card:2")
        self.assertEqual(block_line["outcome"]["incoming_hp_loss_before_hp_cap"], 3)
        self.assertEqual(
            policy["suppressed_zero_current_impact_reasons"]["card:0"],
            "persistent_strength_without_verified_current_turn_hp_damage",
        )

    def test_future_attack_counts_never_keep_a_pure_strength_playable(self):
        state = {
            "player": {
                "hp": 40, "max_hp": 80, "block": 0, "energy": 1, "status": [],
                "hand": [
                    {"id": "INFLAME", "name": "燃烧", "type": "Power", "cost": "1",
                     "description": "Gain 2 Strength.", "index": 0},
                ],
                "draw_pile": [
                    {"id": "STRIKE", "type": "Attack", "description": "Deal 6 damage."},
                ],
                "discard_pile": [],
            },
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "4"}],
            }]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        one_visible_attack = next(
            item for item in policy["suppressed_zero_current_impact_assessments"]
            if item["action_id"] == "card:0"
        )["future_strength_context"]
        self.assertEqual(one_visible_attack["support_status"], "visible_attack_potential")
        self.assertEqual(one_visible_attack["visible_future_attack_cards"], 1)
        self.assertIsNone(one_visible_attack["future_damage_estimate"])
        self.assertTrue(one_visible_attack["uncertain"])

        state["player"]["draw_pile"] = [
            {"id": f"STRIKE_{index}", "type": "Attack", "description": "Deal 6 damage."}
            for index in range(2)
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0", [action.action_id for action in candidates])
        two_hits = next(
            item for item in policy["suppressed_zero_current_impact_assessments"]
            if item["action_id"] == "card:0"
        )["future_strength_context"]
        self.assertEqual(two_hits["support_status"], "visible_attack_potential")
        self.assertEqual(two_hits["visible_future_attack_cards"], 2)
        self.assertEqual(two_hits["visible_future_attack_hit_potential"], 2)
        self.assertTrue(two_hits["visible_attack_hits_meet_setup_support_bar"])
        self.assertIsNone(two_hits["future_damage_estimate"])

    def test_strength_without_same_turn_damage_payoff_is_filtered_even_with_future_attacks(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "INFLAME", "name": "Inflame", "type": "Power", "cost": "1", "description": "Gain 2 Strength.", "index": 0},
                {"id": "STRIKE_A", "name": "Strike A", "type": "Attack", "cost": "1", "description": "Deal 6 damage.", "index": 1},
                {"id": "STRIKE_B", "name": "Strike B", "type": "Attack", "cost": "1", "description": "Deal 6 damage.", "index": 2},
            ], "draw_pile": [
                {"type": "Attack", "description": "Deal 6 damage."}
                for _ in range(4)
            ], "discard_pile": []},
            "battle": {"enemies": [{"entity_id": "E", "hp": 50, "block": 0,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("strength", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("strike-a:target:E", "play_card", "combat", "Play Strike A", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("strike-b:target:E", "play_card", "combat", "Play Strike B", {"action": "play_card", "card_index": 2, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("strength", [action.action_id for action in candidates])
        assessment = next(item for item in policy["suppressed_zero_current_impact_assessments"]
                          if item["action_id"] == "strength")
        self.assertEqual(assessment["attack_sequence_comparison"]["incremental_hp_damage_after_setup"], -4)
        future = assessment["future_strength_context"]
        self.assertEqual(future["minimum_future_attack_hits_to_justify_setup"], 3)
        self.assertEqual(future["visible_future_attack_hit_potential"], 4)
        self.assertTrue(future["visible_attack_hits_meet_setup_support_bar"])

    def test_demon_form_future_turn_strength_does_not_boost_or_authorize_current_attack(self):
        state = {
            "player": {"hp": 32, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "DEMON_FORM", "name": "恶魔形态", "type": "Power", "cost": "1",
                 "description": "在你的回合开始时，获得2点力量。", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 1},
            ], "draw_pile": [
                {"id": f"FUTURE_ATTACK_{index}", "type": "Attack", "description": "造成6点伤害。"}
                for index in range(4)
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 40, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Demon Form", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertNotIn("card:0", {action.action_id for action in candidates})
        self.assertIn("card:1:target:E", {action.action_id for action in candidates})
        demon = next(item for item in policy["suppressed_zero_current_impact_assessments"]
                     if item["action_id"] == "card:0")
        self.assertEqual(demon["strength_timing"], "next_turn_start")
        self.assertFalse(demon["has_verified_attack_hp_payoff_this_turn"])
        self.assertEqual(
            demon["attack_sequence_comparison"]["basis"],
            "deferred_strength_not_applied_this_turn",
        )
        self.assertEqual(
            demon["joint_turn_lines"]["best_enemy_hp_damage_line"]["enemy_hp_damage_estimate"],
            6,
        )
        self.assertNotIn("card:0", policy["provider_candidate_action_ids"])
        self.assertFalse(any(
            "card:0" in item.get("step_action_ids", [])
            for item in policy["turn_line_candidates"]
        ))

    def test_small_attack_vs_hp_trade_is_kept_for_contextual_kev_choice(self):
        state = {
            "player": {"hp": 10, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE", "name": "Strike", "type": "Attack", "cost": "1", "description": "Deal 6 damage.", "index": 0},
                {"id": "DEFEND", "name": "Defend", "type": "Skill", "cost": "1", "description": "Gain 5 Block.", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 0,
                                      "intents": [{"type": "Attack", "label": "5"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        candidate_ids = {action.action_id for action in candidates}
        self.assertIn("card:0:target:E", candidate_ids)
        self.assertIn("card:1", candidate_ids)
        self.assertEqual(policy["policy_revision"], "tempo-risk-setup-v18")
        self.assertFalse(policy["combat_strategy"]["effective_damage_is_a_turn_action_default"])
        selected = {item["first_action_id"]: item["outcome"] for item in policy["turn_line_candidates"]}
        self.assertEqual(selected["card:0:target:E"]["enemy_hp_damage"], 6)
        self.assertEqual(selected["card:0:target:E"]["total_hp_loss_including_self_cost_before_hp_cap"], 5)
        self.assertEqual(selected["card:1"]["enemy_hp_damage"], 0)
        self.assertEqual(selected["card:1"]["total_hp_loss_including_self_cost_before_hp_cap"], 0)
        self.assertEqual(selected["card:0:target:E"]["post_line_hp_reserve_band"], "critical")
        self.assertEqual(selected["card:1"]["post_line_hp_reserve_band"], "usable")
        self.assertEqual(selected["card:0:target:E"]["post_line_hp_reserve_margin_after_same_current_threat"], 0)
        self.assertEqual(selected["card:1"]["post_line_hp_reserve_margin_after_same_current_threat"], 10)
        self.assertIn("not a next-turn forecast", selected["card:0:target:E"]["reserve_band_basis"])
        self.assertIn("never a scalar damage-minus-HP score", policy["turn_outcome_comparison"]["comparison_rule"])

    def test_visible_future_attacks_do_not_rescue_persistent_strength_setup_line(self):
        state = {
            "player": {
                "hp": 40, "max_hp": 80, "block": 0, "energy": 2, "status": [],
                "hand": [
                    {"id": "INFLAME", "name": "Inflame", "type": "Power", "cost": "1", "description": "Gain 2 Strength.", "index": 0},
                    {"id": "STRIKE_A", "name": "Strike A", "type": "Attack", "cost": "1", "description": "Deal 4 damage.", "index": 1},
                    {"id": "STRIKE_B", "name": "Strike B", "type": "Attack", "cost": "1", "description": "Deal 4 damage.", "index": 2},
                    {"id": "DEFEND", "name": "Defend", "type": "Skill", "cost": "1", "description": "Gain 3 Block.", "index": 3},
                ],
                "draw_pile": [
                    {"id": f"FUTURE_ATTACK_{index}", "type": "Attack", "description": "Deal 6 damage."}
                    for index in range(4)
                ],
                "discard_pile": [],
            },
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 100, "block": 0,
                "intents": [{"type": "Attack", "label": "3"}],
            }]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike A", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2:target:E", "play_card", "combat", "Play Strike B", {"action": "play_card", "card_index": 2, "target": "E"}),
            GameAction("card:3", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 3}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertNotIn("card:0", [action.action_id for action in candidates])
        self.assertIn("card:0", policy["suppressed_zero_current_impact_action_ids"])
        assessment = next(item for item in policy["suppressed_zero_current_impact_assessments"]
                          if item["action_id"] == "card:0")
        self.assertEqual(assessment["attack_sequence_comparison"]["incremental_hp_damage_after_setup"], -2)
        self.assertEqual(assessment["future_strength_context"]["visible_future_attack_cards"], 4)
        future = assessment["future_strength_context"]
        self.assertEqual(future["minimum_future_attack_hits_to_repay_current_damage_gap"], 1)
        self.assertEqual(future["minimum_future_attack_hits_to_justify_setup"], 2)
        self.assertTrue(future["visible_attack_hits_meet_setup_support_bar"])
        self.assertFalse(any(item["first_action_id"] == "card:0" for item in policy["turn_line_candidates"]))

    def test_global_line_frontier_compacts_overflow_to_kev_limit(self):
        action = GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"})
        assessment = {
            "action_id": "end-turn",
            "joint_turn_lines": {
                "pareto_tradeoff_lines": [
                    {
                        "line_action_ids": ["end-turn"],
                        "enemy_hp_damage_estimate": value,
                        "projected_total_hp_loss_uncapped_estimate": value,
                        "incoming_hp_loss_before_player_hp_cap_estimate": value,
                        "projected_player_hp_after_turn_end_estimate": 100 - value,
                        "survives_this_turn_estimate": True,
                        "energy_spent_estimate": 0,
                        "cards_played_estimate": 0,
                        "energy_left_estimate": 3,
                        "persistent_strength_added_estimate": 0,
                        "anger_copies_added_to_discard_estimate": 0,
                        "line_effects_uncertain": True,
                        "intent_uncertain": False,
                        "damage_modifiers_uncertain": False,
                        "uncertain": False,
                        "unmodeled_action_ids_excluded": [],
                    }
                    for value in range(300)
                ]
            },
        }

        candidates, removed = _turn_line_choice_candidates({}, [action], [assessment])

        self.assertEqual(len(candidates), 255)
        self.assertEqual(removed, [])
        self.assertEqual(len({item["line_id"] for item in candidates}), 255)

    def test_battle_trance_is_described_as_draw_not_as_a_failed_attack(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 0, "status": [], "hand": [
                {"id": "BATTLE_TRANCE", "name": "Battle Trance", "type": "Skill", "cost": "0",
                 "description": "Draw 3 cards. You cannot draw any more cards this turn.", "index": 0},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 0, "intents": []}]},
        }
        actions = [
            GameAction("battle-trance", "play_card", "combat", "Play Battle Trance", {"action": "play_card", "card_index": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertIn("battle-trance", {action.action_id for action in candidates})
        assessment = next(item for item in policy["action_assessments"] if item["action_id"] == "battle-trance")
        self.assertEqual(assessment["current_tactical_effect_class"], "card_draw_with_unseen_hand_effects")
        self.assertEqual(assessment["cards_drawn_estimate"], 3)
        self.assertTrue(assessment["draw_lock_applied_after_action"])

    def test_future_attack_counts_do_not_rescue_strength_that_loses_current_damage(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 5, "energy": 2, "status": [], "hand": [
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
                {"id": "INFLAME", "name": "燃烧", "type": "Power", "cost": "1", "description": "获得2点力量。", "index": 2},
                {"id": "RAMPAGE", "name": "暴走", "type": "Attack", "cost": "1", "description": "造成9点伤害。将这张牌在本场战斗中的伤害增加5。", "index": 3},
            ], "draw_pile": [
                {"id": "POMMEL_STRIKE", "type": "Attack", "description": "造成9点伤害。"},
                {"id": "IRON_WAVE", "type": "Attack", "description": "造成5点伤害。"},
            ], "discard_pile": []},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 59, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "9"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 2}),
            GameAction("card:3:target:E", "play_card", "combat", "Play Rampage", {"action": "play_card", "card_index": 3, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        all_items = [*policy["action_assessments"], *policy["suppressed_zero_current_impact_assessments"]]
        by_id = {item["action_id"]: item for item in all_items}
        burn = by_id["card:2"]
        strike = by_id["card:0:target:E"]
        burn_line = burn["joint_turn_lines"]["best_enemy_hp_damage_line"]
        strike_line = strike["joint_turn_lines"]["best_enemy_hp_damage_line"]
        self.assertEqual(burn_line["enemy_hp_damage_estimate"], 11)
        self.assertEqual(strike_line["enemy_hp_damage_estimate"], 15)
        self.assertEqual(burn_line["projected_hp_loss_if_turn_ended_estimate"], 4)
        self.assertEqual(strike_line["projected_hp_loss_if_turn_ended_estimate"], 4)
        self.assertEqual(burn["attack_sequence_comparison"]["incremental_hp_damage_after_setup"], -4)
        self.assertIsNone(burn["future_strength_context"]["future_damage_estimate"])
        self.assertEqual(burn["future_strength_context"]["support_status"], "visible_attack_potential")
        self.assertNotIn("card:2", policy["provider_candidate_action_ids"])
        self.assertIn("card:2", policy["suppressed_zero_current_impact_action_ids"])
        self.assertEqual(
            policy["suppressed_zero_current_impact_reasons"]["card:2"],
            "persistent_strength_without_verified_current_turn_hp_damage",
        )
        self.assertEqual(burn_line["line_action_ids"][0], "card:2")
        self.assertEqual(strike_line["line_action_ids"][0], "card:0:target:E")

    def test_joint_turn_lines_show_defend_beats_empty_survival_line(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 20, "block": 0, "status": [],
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        by_id = {item["action_id"]: item for item in policy["action_assessments"]}
        attack_survival = by_id["card:0:target:E"]["joint_turn_lines"]["best_player_hp_preservation_line"]
        block_survival = by_id["card:1"]["joint_turn_lines"]["best_player_hp_preservation_line"]
        attack_frontier = by_id["card:0:target:E"]["joint_turn_lines"]["pareto_tradeoff_lines"]
        self.assertEqual(attack_survival["projected_hp_loss_if_turn_ended_estimate"], 8)
        self.assertEqual(attack_survival["enemy_hp_damage_estimate"], 6)
        self.assertEqual(block_survival["projected_hp_loss_if_turn_ended_estimate"], 3)
        self.assertEqual(block_survival["enemy_hp_damage_estimate"], 0)
        self.assertEqual(
            [(line["enemy_hp_damage_estimate"], line["projected_total_hp_loss_uncapped_estimate"])
             for line in attack_frontier],
            [(6, 8)],
        )

    def test_pareto_lines_keep_both_attack_and_defense_tradeoffs(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "STRIKE_A", "name": "打击 A", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 0},
                {"id": "STRIKE_B", "name": "打击 B", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1", "description": "获得5点格挡。", "index": 2},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike A", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike B", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 2}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        _candidates, policy = combat_candidate_policy(state, actions)
        strike = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        outcomes = strike["joint_turn_lines"]["pareto_tradeoff_lines"]
        pairs = {
            (line["enemy_hp_damage_estimate"], line["projected_total_hp_loss_uncapped_estimate"])
            for line in outcomes
        }
        self.assertIn((12, 8), pairs)
        self.assertIn((6, 3), pairs)
        self.assertEqual(
            strike["joint_turn_lines"]["pareto_tradeoff_objectives"],
            [
                "maximize_enemy_hp_damage", "minimize_total_player_hp_loss",
                "preserve_known_future_effects", "minimize_energy_and_cards_when_outcomes_match",
            ],
        )

    def test_kev_line_candidate_commits_block_clearing_attack_to_its_supported_followup(self):
        state = {
            "player": {"hp": 40, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "JAB_A", "name": "轻刺", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 0},
                {"id": "JAB_B", "name": "重击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 6,
                "intents": [{"type": "Attack", "label": "4"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Jab A", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Jab B", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertIn("card:0:target:E", {action.action_id for action in candidates})
        line = next(
            item for item in policy["turn_line_candidates"]
            if item["first_action_id"] == "card:0:target:E"
            and item["step_action_ids"] == ["card:0:target:E", "card:1:target:E"]
        )
        self.assertEqual(line["outcome"]["enemy_hp_damage"], 6)
        self.assertEqual([step["card_id"] for step in line["steps"]], ["JAB_A", "JAB_B"])

    def test_known_discard_copy_attack_stays_chained_through_its_supported_damage_followup(self):
        state = {
            "player": {"hp": 20, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "ANGER", "name": "愤怒+", "type": "Attack", "cost": "0",
                 "description": "造成8点伤害。将一张此牌的复制品加入你的弃牌堆。", "index": 0},
                {"id": "BASH", "name": "痛击+", "type": "Attack", "cost": "2",
                 "description": "造成8点伤害。施加2层易伤。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 8,
                                      "intents": [{"type": "Attack", "label": "7"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Anger", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Bash", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        line = next(
            item for item in policy["turn_line_candidates"]
            if item["first_action_id"] == "card:0:target:E"
            and item["step_action_ids"] == ["card:0:target:E", "card:1:target:E"]
        )
        self.assertEqual(line["outcome"]["enemy_hp_damage"], 8)
        self.assertEqual([step["card_id"] for step in line["steps"]], ["ANGER", "BASH"])
        self.assertEqual(line["recheck_after_action_id"], "card:1:target:E")

    def test_power_line_carries_measured_setup_payoff_into_choice_context(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "INFLAME", "name": "燃烧", "type": "Power", "cost": "0",
                 "description": "获得2点力量。", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1", "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 40, "block": 0,
                                      "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0", "play_card", "combat", "Play Inflame", {"action": "play_card", "card_index": 0}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        _candidates, policy = combat_candidate_policy(state, actions)
        line = next(item for item in policy["turn_line_candidates"] if item["first_action_id"] == "card:0")
        payoff = line["first_action_context"]["setup_payoff_evidence"]
        self.assertEqual(payoff["current_turn_hp_damage_with_setup"], 8)
        self.assertEqual(payoff["current_turn_incremental_hp_damage"], 2)

    def test_setup_strike_applies_its_temporary_strength_after_its_own_hit(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "SETUP_STRIKE", "name": "预备打击+", "type": "Attack", "cost": "1",
                 "description": "造成9点伤害。在本回合内获得3点力量。", "index": 0},
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 50, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Setup Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        _candidates, policy = combat_candidate_policy(state, actions)
        setup = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        line = setup["joint_turn_lines"]["best_enemy_hp_damage_line"]

        self.assertEqual(line["line_action_ids"], ["card:0:target:E", "card:1:target:E"])
        self.assertEqual(line["enemy_hp_damage_estimate"], 18)
        self.assertEqual(line["strength_applied_to_current_attack_line_estimate"], 3)
        self.assertFalse(line["uncertain"])
        self.assertEqual(setup["strength_gain_from_live_text"], 3)

    def test_strict_same_turn_dominator_removes_wasteful_expensive_first_attack(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "HEAVY_STRIKE", "name": "重击", "type": "Attack", "cost": "2",
                 "description": "造成4点伤害。", "index": 0},
                {"id": "JAB_A", "name": "轻刺", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 1},
                {"id": "JAB_B", "name": "快速打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 2},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Heavy Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Jab A", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2:target:E", "play_card", "combat", "Play Jab B", {"action": "play_card", "card_index": 2, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        ids = [item.action_id for item in candidates]
        self.assertNotIn("card:0:target:E", ids)
        self.assertIn("card:1:target:E", ids)
        proof = policy["suppressed_dominated_action_proofs"]["card:0:target:E"]
        self.assertEqual(proof["dominator_action_id"], "card:1:target:E")
        self.assertEqual(proof["dominator_damage"], 12)
        self.assertEqual(proof["dominated_best_damage"], 4)

    def test_meaningful_attack_and_defense_remain_a_real_tradeoff_for_kev(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 0},
                {"id": "DEFEND", "name": "防御", "type": "Skill", "cost": "1",
                 "description": "获得5点格挡。", "index": 1},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        ids = [item.action_id for item in candidates]
        self.assertIn("card:0:target:E", ids)
        self.assertIn("card:1", ids)
        self.assertNotIn("card:0:target:E", policy["suppressed_dominated_action_ids"])
        selected = {item["first_action_id"]: item["outcome"] for item in policy["turn_line_candidates"]}
        self.assertEqual(selected["card:0:target:E"]["enemy_hp_damage"], 6)
        self.assertEqual(selected["card:0:target:E"]["total_hp_loss_including_self_cost_before_hp_cap"], 8)
        self.assertEqual(selected["card:1"]["enemy_hp_damage"], 0)
        self.assertEqual(selected["card:1"]["total_hp_loss_including_self_cost_before_hp_cap"], 3)

    def test_unmodeled_potion_stays_separate_from_exact_line_comparison(self):
        state = {
            "player": {"hp": 30, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "HEAVY_STRIKE", "name": "重击", "type": "Attack", "cost": "2",
                 "description": "造成4点伤害。", "index": 0},
                {"id": "JAB_A", "name": "轻刺", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 1},
                {"id": "JAB_B", "name": "快速打击", "type": "Attack", "cost": "1",
                 "description": "造成6点伤害。", "index": 2},
            ]},
            "battle": {"enemies": [{
                "entity_id": "E", "hp": 30, "block": 0,
                "intents": [{"type": "Attack", "label": "8"}],
            }]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Heavy Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Jab A", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2:target:E", "play_card", "combat", "Play Jab B", {"action": "play_card", "card_index": 2, "target": "E"}),
            GameAction("potion:0", "use_potion", "combat", "Use a combat potion", {"action": "use_potion", "slot": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]
        candidates, policy = combat_candidate_policy(state, actions)
        self.assertNotIn("card:0:target:E", [item.action_id for item in candidates])
        self.assertIn("potion:0", [item.action_id for item in candidates])
        self.assertIn("card:0:target:E", policy["suppressed_dominated_turn_line_first_action_ids"])
        heavy = next(item for item in policy["action_assessments"] if item["action_id"] == "card:0:target:E")
        self.assertTrue(heavy["joint_turn_lines"]["uncertain"])
        self.assertIn("potion:0", heavy["joint_turn_lines"]["unmodeled_action_ids_excluded"])
        self.assertTrue(any(
            item["first_action_id"] == "card:0:target:E"
            and item["basis"] in {
                "strict_current_turn_damage_and_hp_dominance",
            }
            for item in policy["suppressed_dominated_turn_lines"]
        ))

    def test_main_setup_strike_is_applied_after_its_own_damage(self):
        state = {
            "player": {"hp": 40, "max_hp": 80, "block": 0, "energy": 2, "status": [], "hand": [
                {"id": "SetupStrike", "name": "预备打击", "type": "Attack", "cost": "1",
                 "description": "Deal 7 damage. Gain 2 Setup Strike.", "index": 0},
                {"id": "STRIKE_IRONCLAD", "name": "打击", "type": "Attack", "cost": "1",
                 "description": "Deal 6 damage.", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 13,
                "intents": [{"type": "Attack", "label": "4"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Setup Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)
        self.assertIn("card:0:target:E", [action.action_id for action in candidates])
        setup_line = next(
            item for item in policy["turn_line_candidates"]
            if item["first_action_id"] == "card:0:target:E"
            and item["step_action_ids"][:2] == ["card:0:target:E", "card:1:target:E"]
        )
        self.assertTrue(setup_line["outcome"]["selected_line_outcome_is_exact"])
        self.assertEqual(setup_line["outcome"]["enemy_hp_damage"], 2)

    def test_setup_strike_into_enemy_block_loses_to_block_when_it_deals_no_hp_damage(self):
        state = {
            "player": {"hp": 37, "max_hp": 80, "block": 0, "energy": 2, "status": [],
                "potions": [{"id": "unknown_potion", "name": "Unknown potion"}],
                "hand": [
                    {"id": "SetupStrike", "name": "预备打击", "type": "Attack", "cost": "1",
                     "description": "Deal 7 damage. Gain 2 Setup Strike.", "index": 0},
                    {"id": "STRIKE_IRONCLAD", "name": "打击", "type": "Attack", "cost": "1",
                     "description": "造成6点伤害。", "index": 1},
                    {"id": "DEFEND_IRONCLAD", "name": "防御", "type": "Skill", "cost": "1",
                     "description": "获得5点格挡。", "index": 2},
                ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 30, "block": 15,
                "status": [], "intents": [{"type": "Attack", "label": "8"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Setup Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 1, "target": "E"}),
            GameAction("card:2", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 2}),
            GameAction("potion:0", "use_potion", "combat", "Use unknown potion", {"action": "use_potion", "slot": 0}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertNotIn("card:0:target:E", [action.action_id for action in candidates])
        self.assertIn("card:2", [action.action_id for action in candidates])
        self.assertIn("potion:0", [action.action_id for action in candidates])
        self.assertIn("card:0:target:E", policy["suppressed_no_hp_damage_attack_action_ids"])
        self.assertEqual(
            policy["suppressed_no_hp_damage_attack_reasons"]["card:0:target:E"],
            "complete_supported_line_only_removes_enemy_block_and_deals_no_enemy_hp_damage",
        )
        setup_action_assessment = next(
            item for item in policy["suppressed_no_hp_damage_attack_assessments"]
            if item["action_id"] == "card:0:target:E"
        )
        setup_line = setup_action_assessment["joint_turn_lines"]
        modeled_lines = setup_line["pareto_tradeoff_lines"]
        self.assertTrue(modeled_lines)
        setup_sequence = next(
            line for line in modeled_lines
            if line["line_action_ids"][:2] == ["card:0:target:E", "card:2"]
        )
        self.assertEqual(setup_sequence["enemy_hp_damage_estimate"], 0)
        self.assertFalse(setup_sequence["line_effects_uncertain"])
        self.assertEqual(setup_sequence["energy_spent_estimate"], 2)
        self.assertEqual(setup_sequence["persistent_strength_added_estimate"], 0)
        defend_line = next(
            item for item in policy["turn_line_candidates"]
            if item["first_action_id"] == "card:2"
        )
        self.assertEqual(defend_line["outcome"]["enemy_hp_damage"], 0)
        self.assertEqual(defend_line["outcome"]["total_hp_loss_including_self_cost_before_hp_cap"], 3)
        self.assertTrue(defend_line["outcome"]["selected_line_outcome_is_exact"])
        self.assertTrue(defend_line["outcome"]["search_uncertain"])

    def test_surviving_exact_line_beats_higher_damage_line_that_would_kill_player(self):
        state = {
            "player": {"hp": 10, "max_hp": 80, "block": 0, "energy": 1, "status": [], "hand": [
                {"id": "STRIKE", "name": "Strike", "type": "Attack", "cost": "1",
                 "description": "Deal 12 damage.", "index": 0},
                {"id": "DEFEND", "name": "Defend", "type": "Skill", "cost": "1",
                 "description": "Gain 5 Block.", "index": 1},
            ]},
            "battle": {"enemies": [{"entity_id": "E", "hp": 40, "block": 0,
                "intents": [{"type": "Attack", "label": "12"}]}]},
        }
        actions = [
            GameAction("card:0:target:E", "play_card", "combat", "Play Strike", {"action": "play_card", "card_index": 0, "target": "E"}),
            GameAction("card:1", "play_card", "combat", "Play Defend", {"action": "play_card", "card_index": 1}),
            GameAction("end-turn", "end_turn", "combat", "End turn", {"action": "end_turn"}),
        ]

        candidates, policy = combat_candidate_policy(state, actions)

        self.assertEqual([action.action_id for action in candidates], ["card:1"])
        self.assertEqual(policy["turn_line_candidates"][0]["outcome"]["survives"], True)
        self.assertTrue(any(
            item["first_action_id"] == "card:0:target:E"
            and item["basis"] == "exact_surviving_line_beats_lethal_line"
            for item in policy["suppressed_dominated_turn_lines"]
        ))

    def test_rejects_weak_blocked_multihit_or_nonattacking_target_as_certain_kill(self):
        variations = []
        weak = nibbit_state()
        weak["player"]["status"] = [{"id": "WEAK", "type": "Debuff", "amount": 1}]
        variations.append(weak)
        blocked = nibbit_state()
        blocked["battle"]["enemies"][0]["block"] = 1
        variations.append(blocked)
        multi_hit = nibbit_state()
        for card in multi_hit["player"]["hand"]:
            if card["type"] == "Attack":
                card["description"] = "造成3点伤害2次。"
        variations.append(multi_hit)
        no_attack_intent = nibbit_state()
        no_attack_intent["battle"]["enemies"][0]["intents"] = [{"type": "Defend", "label": ""}]
        variations.append(no_attack_intent)
        for state in variations:
            candidates, policy = combat_candidate_policy(state, nibbit_actions())
            self.assertNotEqual(policy["mode"], "guaranteed_attacker_kill")
            self.assertGreater(len(candidates), 0)


if __name__ == "__main__":
    unittest.main()
