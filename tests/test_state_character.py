import unittest

from sts2_agent.state import normalize_state
from sts2_agent.actions import derive_actions
from sts2_agent.controller import Controller


def map_state(player: dict) -> dict:
    return {
        "bridge_schema_version": 2,
        "state_type": "map",
        "run": {"act": 1, "floor": 0},
        "player": player,
        "map": {"next_options": []},
    }


class CharacterNormalizationTests(unittest.TestCase):
    def test_character_id_is_preferred_to_localized_title(self):
        snapshot = normalize_state(
            map_state({"character": "localized display title", "character_id": "IRONCLAD"}),
            "v0.107.1",
        )
        self.assertEqual(snapshot.character, "Ironclad")

    def test_ironclad_is_recovered_from_corrupt_title_and_live_starting_relic_id(self):
        snapshot = normalize_state(
            map_state({
                "character": "����սʿ",
                "relics": [{"id": "BURNING_BLOOD", "name": "localized title"}],
            }),
            "v0.107.1",
        )
        self.assertEqual(snapshot.character, "Ironclad")

    def test_unreadable_character_without_verified_relic_stays_unrecognized(self):
        snapshot = normalize_state(
            map_state({"character": "����", "relics": [{"id": "UNKNOWN_RELIC"}]}),
            "v0.107.1",
        )
        self.assertEqual(snapshot.character, "����")

    def test_other_character_id_is_not_mistaken_for_ironclad(self):
        snapshot = normalize_state(
            map_state({"character": "The Silent", "character_id": "SILENT"}),
            "v0.107.1",
        )
        self.assertEqual(snapshot.character, "Silent")

    def test_terminal_game_over_screen_is_complete_without_run_or_player_payload(self):
        snapshot = normalize_state({
            "bridge_schema_version": 2,
            "state_type": "game_over",
            "game_over": {"message": "Run ended.", "options": ["main_menu"]},
        }, "v0.107.1")
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.errors, ())
        self.assertEqual(snapshot.state_type, "game_over")
        actions, errors = derive_actions(snapshot)
        self.assertEqual(errors, [])
        self.assertEqual([action.action_id for action in actions], ["menu:game_over:main_menu"])
        self.assertEqual(actions[0].payload, {"action": "menu_select", "option": "main_menu"})
        self.assertTrue(Controller._menu_option_enabled(snapshot.raw, "main_menu"))
        self.assertFalse(Controller._menu_option_enabled(snapshot.raw, "continue"))


if __name__ == "__main__":
    unittest.main()
