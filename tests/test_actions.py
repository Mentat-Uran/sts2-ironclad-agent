import unittest

from sts2_agent.actions import derive_actions
from sts2_agent.models import GameSnapshot


class PotionActionTests(unittest.TestCase):
    def test_any_player_potion_maps_to_self_in_single_player_combat(self):
        snapshot = GameSnapshot(
            state_type="monster",
            raw={
                "state_type": "monster",
                "battle": {"enemies": [{"entity_id": "ENEMY_0", "hp": 10}]},
                "player": {
                    "hand": [],
                    "potions": [{
                        "id": "STRENGTH_POTION",
                        "name": "Strength Potion",
                        "description": "Gain 2 Strength.",
                        "slot": 0,
                        "can_use_in_combat": True,
                        "target_type": "AnyPlayer",
                    }],
                },
            },
            state_fingerprint="test-state",
            game_version="v0.107.1",
            character="Ironclad",
            complete=True,
        )

        actions, errors = derive_actions(snapshot)

        potion = next(action for action in actions if action.action == "use_potion")
        self.assertEqual(potion.action_id, "potion:0")
        self.assertEqual(potion.payload, {"action": "use_potion", "slot": 0})
        self.assertEqual(errors, [])


class CombatCardTargetTests(unittest.TestCase):
    def test_random_enemy_card_is_targetless_and_left_to_game_rng(self):
        snapshot = GameSnapshot(
            state_type="monster",
            raw={
                "state_type": "monster",
                "battle": {"enemies": [
                    {"entity_id": "ENEMY_0", "name": "A", "hp": 10},
                    {"entity_id": "ENEMY_1", "name": "B", "hp": 10},
                ]},
                "player": {"hand": [{
                    "index": 0,
                    "id": "SWORD_BOOMERANG",
                    "name": "Sword Boomerang",
                    "type": "Attack",
                    "cost": "1",
                    "description": "Deal 3 damage 3 times to a random enemy.",
                    "target_type": "RandomEnemy",
                    "can_play": True,
                }]},
            },
            state_fingerprint="random-target-state",
            game_version="v0.107.1",
            character="Ironclad",
            complete=True,
        )

        actions, errors = derive_actions(snapshot)

        card_action = next(action for action in actions if action.action == "play_card")
        self.assertEqual(card_action.action_id, "card:0")
        self.assertEqual(card_action.payload, {"action": "play_card", "card_index": 0})
        self.assertIsNone(card_action.payload.get("target"))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
