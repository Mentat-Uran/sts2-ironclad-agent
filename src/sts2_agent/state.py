from __future__ import annotations

from typing import Any

from .models import GameSnapshot, fingerprint

COMBAT_SCREENS = {"monster", "elite", "boss"}
TERMINAL_SCREENS = {"victory", "run_complete", "campaign_complete", "defeat", "death", "game_over", "run_over"}
SUPPORTED_SCREENS = COMBAT_SCREENS | {
    "hand_select", "rewards", "card_reward", "map", "event", "rest_site", "shop", "menu",
    "fake_merchant", "treasure", "card_select", "bundle_select", "relic_select",
    "crystal_sphere",
} | TERMINAL_SCREENS

_CHARACTER_IDS = {
    "IRONCLAD": "Ironclad",
    "SILENT": "Silent",
    "REGENT": "Regent",
    "NECROBINDER": "Necrobinder",
    "DEFECT": "Defect",
}


def _character_name(player: dict[str, Any]) -> str | None:
    character_id = player.get("character_id") or player.get("characterId")
    if isinstance(character_id, str) and character_id.upper() in _CHARACTER_IDS:
        return _CHARACTER_IDS[character_id.upper()]

    character = player.get("character")
    if character is None:
        return None
    text = str(character)
    aliases = {
        "ironclad": "Ironclad",
        "the ironclad": "Ironclad",
        "铁甲战士": "Ironclad",
        "silent": "Silent",
        "the silent": "Silent",
        "regent": "Regent",
        "the regent": "Regent",
        "necrobinder": "Necrobinder",
        "the necrobinder": "Necrobinder",
        "defect": "Defect",
        "the defect": "Defect",
    }
    if text.casefold() in aliases:
        return aliases[text.casefold()]

    # The current v0.107.1 Chinese localization arrives from the game bridge with
    # U+FFFD characters in Character.Title. Recover only from a stable game ID:
    # the live character-select data maps Ironclad to BURNING_BLOOD, and the run
    # state exposes that same relic ID. Arbitrary/unreadable names stay unknown.
    relics = player.get("relics")
    relic_ids = {
        relic.get("id")
        for relic in relics
        if isinstance(relic, dict) and isinstance(relic.get("id"), str)
    } if isinstance(relics, list) else set()
    if "\ufffd" in text and "BURNING_BLOOD" in relic_ids:
        return "Ironclad"
    return text


def normalize_state(raw: dict[str, Any], game_version: str | None) -> GameSnapshot:
    errors: list[str] = []
    if raw.get("bridge_schema_version") != 2:
        errors.append("Game bridge schema is missing or unsupported; passive state schema 2 is required.")
    state_type = str(raw.get("state_type", "unknown"))
    player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
    character = _character_name(player)

    if state_type == "menu":
        menu_screen = raw.get("menu_screen")
        options = raw.get("options")
        if menu_screen != "main":
            errors.append(f"Menu screen {menu_screen!r} is not supported for safe run continuation.")
        elif not isinstance(options, list):
            errors.append("Main menu state is missing its advertised options.")
        elif not any(
            (isinstance(item, str) and item.casefold() == "continue")
            or (isinstance(item, dict) and str(item.get("name", "")).casefold() == "continue" and item.get("enabled") is True)
            for item in options
        ):
            errors.append("Main menu does not advertise a resumable run; no menu action is available.")
    elif state_type not in SUPPORTED_SCREENS:
        errors.append(f"Unsupported game screen: {state_type}.")

    if state_type in SUPPORTED_SCREENS and state_type not in TERMINAL_SCREENS and state_type != "menu":
        if not isinstance(raw.get("run"), dict):
            errors.append("In-run state is missing run metadata.")
        if not isinstance(raw.get("player"), dict):
            errors.append("In-run state is missing player data.")

    if state_type in COMBAT_SCREENS:
        battle = raw.get("battle")
        if not isinstance(player, dict) or not isinstance(battle, dict):
            errors.append("Combat state is missing player or battle data.")
        else:
            if not isinstance(player.get("hand"), list) or not isinstance(battle.get("enemies"), list):
                errors.append("Combat state is missing hand or enemy data.")
            if battle.get("turn") != "player" or battle.get("is_play_phase") is not True:
                errors.append("Combat is not in a ready player action phase.")

    if state_type == "map":
        map_data = raw.get("map")
        if not isinstance(map_data, dict) or not isinstance(map_data.get("next_options"), list):
            errors.append("Map state is missing next_options.")

    if state_type == "shop":
        shop = raw.get("shop")
        if not isinstance(shop, dict) or not isinstance(shop.get("items"), list):
            errors.append("Shop state is missing inventory items.")
        elif (
            not isinstance(shop.get("inventory_open"), bool)
            or not isinstance(shop.get("can_close_inventory"), bool)
            or not isinstance(shop.get("can_proceed"), bool)
        ):
            errors.append("Shop state is missing inventory-open, close, or proceed controls.")

    if state_type == "fake_merchant":
        merchant = raw.get("fake_merchant")
        shop = merchant.get("shop") if isinstance(merchant, dict) else None
        if not isinstance(shop, dict) or not isinstance(shop.get("items"), list):
            errors.append("Fake merchant state is missing nested shop inventory.")
        elif (
            not isinstance(shop.get("inventory_open"), bool)
            or not isinstance(shop.get("can_close_inventory"), bool)
            or not isinstance(shop.get("can_proceed"), bool)
        ):
            errors.append("Fake merchant state is missing inventory-open, close, or proceed controls.")

    if state_type == "treasure":
        treasure = raw.get("treasure")
        if not isinstance(treasure, dict) or not isinstance(treasure.get("relics"), list):
            errors.append("Treasure state is missing its relic list.")
        elif any(not isinstance(treasure.get(key), bool) for key in ("chest_open", "can_open_chest", "can_proceed")):
            errors.append("Treasure state is missing chest-open, open, or proceed controls.")

    if state_type == "card_reward":
        reward = raw.get("card_reward")
        if not isinstance(reward, dict) or not isinstance(reward.get("cards"), list):
            errors.append("Card reward state is missing the offered cards.")

    if state_type == "rewards":
        reward = raw.get("rewards")
        if not isinstance(reward, dict) or not isinstance(reward.get("items"), list):
            errors.append("Reward state is missing the claimable reward list.")
        elif not reward.get("items") and reward.get("can_proceed") is not True:
            errors.append("Reward screen is still loading its items and cannot proceed yet.")

    if state_type == "event":
        event = raw.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("in_dialogue"), bool):
            errors.append("Event state is missing its dialogue/choice phase marker.")
        elif event.get("in_dialogue") is not True and not isinstance(event.get("options"), list):
            errors.append("Event state is missing its choice list.")
        elif event.get("in_dialogue") is not True and not event.get("options"):
            errors.append("Event choice list is still loading or empty.")
        elif (
            event.get("in_dialogue") is not True
            and len(event.get("options", [])) == 1
            and isinstance(event["options"][0], dict)
            and event["options"][0].get("is_proceed") is True
            and event["options"][0].get("was_chosen") is True
        ):
            errors.append("Event result is still transitioning to its next screen or choice.")

    if state_type == "card_select":
        selection = raw.get("card_select")
        if not isinstance(selection, dict) or not isinstance(selection.get("cards"), list):
            errors.append("Deck-card selection is missing its card list.")
        elif selection.get("preview_showing") is True and (
            not isinstance(selection.get("preview_cards"), list)
            or not (selection.get("can_confirm") is True or selection.get("can_cancel") is True)
        ):
            errors.append("Deck-card preview is incomplete or has no available confirmation control.")

    complete = not errors
    return GameSnapshot(
        state_type=state_type,
        raw=raw,
        state_fingerprint=fingerprint(raw),
        game_version=game_version,
        character=str(character) if character else None,
        complete=complete,
        errors=tuple(errors),
    )
