from __future__ import annotations

from typing import Any

from .models import GameAction, GameSnapshot
from .state import TERMINAL_SCREENS


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _potion_slot_available(raw: dict[str, Any]) -> bool:
    player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
    potions = player.get("potions")
    max_slots = player.get("max_potion_slots")
    return (
        isinstance(potions, list)
        and isinstance(max_slots, int)
        and max_slots >= 0
        and len(potions) < max_slots
    )


def _index(item: dict[str, Any], fallback: int) -> int | None:
    value = item.get("index", fallback)
    return value if isinstance(value, int) and value >= 0 else None


def _add_option_actions(
    out: list[GameAction],
    *,
    entries: list[dict[str, Any]],
    prefix: str,
    action_name: str,
    index_key: str,
    phase: str,
    description: Any,
    strategic: bool = True,
    include: Any = None,
) -> None:
    for fallback, item in enumerate(entries):
        index = _index(item, fallback)
        if index is None or (include is not None and not include(item)):
            continue
        label = description(item)
        out.append(GameAction(
            action_id=f"{prefix}:{index}",
            action=action_name,
            phase=phase,
            description=str(label),
            payload={"action": action_name, index_key: index},
            strategic=strategic,
        ))


def derive_actions(snapshot: GameSnapshot) -> tuple[list[GameAction], list[str]]:
    """Build only actions represented by the upstream game's current screen data."""
    raw = snapshot.raw
    screen = snapshot.state_type
    out: list[GameAction] = []
    errors: list[str] = []

    if screen in TERMINAL_SCREENS:
        if screen == "game_over":
            game_over = raw.get("game_over") if isinstance(raw.get("game_over"), dict) else {}
            options = game_over.get("options")
            if not isinstance(options, list):
                errors.append("Game-over state is missing its advertised menu options.")
            else:
                for option in options:
                    if option != "main_menu":
                        errors.append(f"Unsupported game-over menu option {option!r}; refusing to guess.")
                        continue
                    out.append(GameAction(
                        action_id="menu:game_over:main_menu", action="menu_select", phase="menu",
                        description="Return to the main menu from the completed run.",
                        payload={"action": "menu_select", "option": "main_menu"}, strategic=False,
                    ))
        return out, errors

    if screen == "menu":
        menu_screen = raw.get("menu_screen")
        options = raw.get("options")
        continue_enabled = isinstance(options, list) and any(
            (isinstance(item, str) and item.casefold() == "continue")
            or (isinstance(item, dict) and str(item.get("name", "")).casefold() == "continue" and item.get("enabled") is True)
            for item in options
        )
        if menu_screen == "main" and continue_enabled:
            out.append(GameAction(
                action_id="menu:main:continue", action="menu_select", phase="menu",
                description="Continue the currently advertised saved run.",
                payload={"action": "menu_select", "option": "continue"}, strategic=False,
            ))
        else:
            errors.append("Safe continuation requires the main menu to advertise an enabled Continue option.")
        return out, errors

    if screen in {"monster", "elite", "boss"}:
        player = raw.get("player", {})
        battle = raw.get("battle", {})
        enemies = [e for e in _items(battle.get("enemies")) if int(e.get("hp", 0) or 0) > 0]
        for i, card in enumerate(_items(player.get("hand"))):
            if card.get("can_play") is not True:
                continue
            target_type = str(card.get("target_type", "None"))
            base = {"action": "play_card", "card_index": _index(card, i)}
            if target_type == "AnyEnemy":
                if not enemies:
                    errors.append(f"Playable card {card.get('name', i)} needs an enemy target but no living enemy is visible.")
                for enemy in enemies:
                    target = enemy.get("entity_id")
                    if not isinstance(target, str) or not target:
                        errors.append("A living enemy has no stable entity_id for a targeted card.")
                        continue
                    payload = dict(base, target=target)
                    out.append(GameAction(
                        action_id=f"card:{i}:target:{target}", action="play_card", phase="combat",
                        description=f"Play {card.get('name', f'card {i}')} on {enemy.get('name', target)}; {card.get('description', '')}",
                        payload=payload, strategic=False,
                    ))
            # RandomEnemy is deliberately targetless: the game chooses a living
            # enemy during resolution. The installed Mod's ExecutePlayCard passes
            # null for every card target type except AnyEnemy, matching PlayCardAction.
            elif target_type in {"None", "Self", "AllEnemies", "RandomEnemy"}:
                out.append(GameAction(
                    action_id=f"card:{i}", action="play_card", phase="combat",
                    description=f"Play {card.get('name', f'card {i}')}; {card.get('description', '')}",
                    payload=base, strategic=False,
                ))
            else:
                errors.append(f"Unsupported playable-card target type {target_type!r}; pausing rather than guessing.")

        for fallback, potion in enumerate(_items(player.get("potions"))):
            if potion.get("can_use_in_combat") is not True:
                continue
            slot = potion.get("slot", fallback)
            if not isinstance(slot, int) or slot < 0:
                errors.append("A usable potion has an invalid slot index.")
                continue
            target_type = str(potion.get("target_type", "None"))
            base = {"action": "use_potion", "slot": slot}
            if target_type == "AnyEnemy":
                for enemy in enemies:
                    target = enemy.get("entity_id")
                    if isinstance(target, str) and target:
                        out.append(GameAction(
                            action_id=f"potion:{slot}:target:{target}", action="use_potion", phase="combat",
                            description=f"Use {potion.get('name', 'potion')} on {enemy.get('name', target)}; {potion.get('description', '')}",
                            payload=dict(base, target=target), strategic=False,
                        ))
                    else:
                        errors.append("A potion target has no stable entity_id.")
            # The upstream single-player Mod resolves AnyPlayer to the local player
            # creature (McpMod.Actions.cs, ExecuteUsePotion). No target ID is needed.
            elif target_type in {"None", "Self", "AnyPlayer", "AllEnemies"}:
                out.append(GameAction(
                    action_id=f"potion:{slot}", action="use_potion", phase="combat",
                    description=f"Use {potion.get('name', 'potion')}; {potion.get('description', '')}",
                    payload=base, strategic=False,
                ))
            else:
                errors.append(f"Unsupported usable-potion target type {target_type!r}; pausing rather than guessing.")

        out.append(GameAction(
            action_id="end-turn", action="end_turn", phase="combat",
            description="End the player turn.", payload={"action": "end_turn"}, strategic=False,
        ))

    elif screen == "hand_select":
        select = raw.get("hand_select", {})
        _add_option_actions(out, entries=_items(select.get("cards")), prefix="hand-select",
                            action_name="combat_select_card", index_key="card_index", phase="combat-selection",
                            description=lambda x: f"Select {x.get('name', 'card')}: {x.get('description', '')}", strategic=False)
        if select.get("can_confirm") is True:
            out.append(GameAction("confirm-hand-selection", "combat_confirm_selection", "combat-selection",
                                  "Confirm the selected combat cards.", {"action": "combat_confirm_selection"}, False))

    elif screen == "card_reward":
        reward = raw.get("card_reward", {})
        _add_option_actions(out, entries=_items(reward.get("cards")), prefix="reward-card",
                            action_name="select_card_reward", index_key="card_index", phase="card-reward",
                            description=lambda x: f"Add {x.get('name', 'card')} ({x.get('rarity', 'rarity unknown')}): {x.get('description', '')}")
        if reward.get("can_skip") is True:
            out.append(GameAction("skip-card-reward", "skip_card_reward", "card-reward",
                                  "Skip adding a card to the deck.", {"action": "skip_card_reward"}, True))

    elif screen == "rewards":
        reward = raw.get("rewards", {})
        _add_option_actions(out, entries=_items(reward.get("items")), prefix="reward",
                            action_name="claim_reward", index_key="index", phase="rewards",
                            description=lambda x: f"Claim {x.get('type', 'reward')}: {x.get('description', '')}",
                            strategic=False,
                            include=lambda x: str(x.get("type", "")).casefold() != "potion" or _potion_slot_available(raw))
        if reward.get("can_proceed") is True:
            out.append(GameAction("proceed-rewards", "proceed", "rewards", "Leave rewards and proceed to the map.",
                                  {"action": "proceed"}, True))

    elif screen == "map":
        map_data = raw.get("map", {})
        for fallback, node in enumerate(_items(map_data.get("next_options"))):
            index = _index(node, fallback)
            if index is None:
                errors.append("A reachable map node has an invalid index.")
                continue
            coords = f"row {node.get('row', '?')}, column {node.get('col', '?')}"
            lookahead = ", ".join(str(x.get("type", "?")) for x in _items(node.get("leads_to")))
            suffix = f"; next floor can lead to {lookahead}" if lookahead else ""
            out.append(GameAction(
                action_id=f"map-node:{node.get('row')}:{node.get('col')}", action="choose_map_node", phase="map-step",
                description=f"Travel to {node.get('type', 'unknown')} at {coords}{suffix}.",
                payload={"action": "choose_map_node", "index": index}, strategic=False,
            ))

    elif screen == "shop" or screen == "fake_merchant":
        shop = raw.get("shop", {}) if screen == "shop" else raw.get("fake_merchant", {}).get("shop", {})
        potion_slot_available = _potion_slot_available(raw)
        _add_option_actions(out, entries=_items(shop.get("items")), prefix="shop-item",
                            action_name="shop_purchase", index_key="index", phase="shop",
                            description=lambda x: f"Buy {x.get('category', 'item')} {x.get('card_name') or x.get('relic_name') or x.get('potion_name') or ''} for {x.get('price', x.get('cost', '?'))} gold; {x.get('card_description') or x.get('relic_description') or x.get('potion_description') or ''}",
                            include=lambda x: (
                                x.get("is_stocked") is True
                                and x.get("can_afford") is True
                                and (str(x.get("category", "")).casefold() != "potion" or potion_slot_available)
                            ))
        if shop.get("inventory_open") is True and shop.get("can_close_inventory") is True:
            out.append(GameAction(
                "close-shop-inventory", "close_shop_inventory", "shop-controls",
                "Close the open shop inventory.", {"action": "close_shop_inventory"}, False,
            ))
        if shop.get("inventory_open") is False and shop.get("can_proceed") is True:
            out.append(GameAction("leave-shop", "proceed", "shop", "Leave the shop.", {"action": "proceed"}, False))

    elif screen == "event":
        event = raw.get("event", {})
        if event.get("in_dialogue") is True:
            out.append(GameAction("advance-event-dialogue", "advance_dialogue", "event-dialogue",
                                  "Advance the current event dialogue; no option is selected.",
                                  {"action": "advance_dialogue"}, False))
        else:
            _add_option_actions(out, entries=_items(event.get("options")), prefix="event-option",
                                action_name="choose_event_option", index_key="index", phase="event",
                                description=lambda x: f"{x.get('title', 'Option')}: {x.get('description', '')}",
                                include=lambda x: x.get("is_locked") is not True)

    elif screen == "rest_site":
        rest = raw.get("rest_site", {})
        _add_option_actions(out, entries=_items(rest.get("options")), prefix="rest-option",
                            action_name="choose_rest_option", index_key="index", phase="rest-site",
                            description=lambda x: f"{x.get('name', x.get('id', 'Option'))}: {x.get('description', '')}",
                            include=lambda x: x.get("is_enabled") is True)
        if rest.get("can_proceed") is True:
            out.append(GameAction("proceed-rest", "proceed", "rest-site", "Leave the rest site.", {"action": "proceed"}, True))

    elif screen == "treasure":
        treasure = raw.get("treasure", {})
        if treasure.get("can_open_chest") is True:
            out.append(GameAction(
                "open-treasure-chest", "open_treasure_chest", "treasure",
                "Open the closed treasure chest.", {"action": "open_treasure_chest"}, False,
            ))
        elif treasure.get("chest_open") is True:
            _add_option_actions(out, entries=_items(treasure.get("relics")), prefix="treasure-relic",
                                action_name="claim_treasure_relic", index_key="index", phase="treasure",
                                description=lambda x: f"Claim {x.get('name', 'relic')}: {x.get('description', '')}")
        if treasure.get("can_proceed") is True:
            out.append(GameAction("proceed-treasure", "proceed", "treasure", "Leave the treasure room.", {"action": "proceed"}, True))

    elif screen == "relic_select":
        choice = raw.get("relic_select", {})
        _add_option_actions(out, entries=_items(choice.get("relics")), prefix="relic-choice",
                            action_name="select_relic", index_key="index", phase="relic-select",
                            description=lambda x: f"Choose {x.get('name', 'relic')}: {x.get('description', '')}")
        if choice.get("can_skip") is True:
            out.append(GameAction("skip-relic-choice", "skip_relic_selection", "relic-select",
                                  "Skip the relic selection.", {"action": "skip_relic_selection"}, True))

    elif screen == "card_select":
        selection = raw.get("card_select", {})
        if selection.get("preview_showing") is True:
            # The selected card is already previewed; reselecting its grid index only
            # replays the same input. Commit the selected preview directly.
            if selection.get("can_confirm") is True:
                out.append(GameAction("confirm-deck-selection", "confirm_selection", "card-select",
                                      "Confirm the selected deck-card preview.", {"action": "confirm_selection"}, False))
            elif selection.get("can_cancel") is True:
                out.append(GameAction("cancel-deck-selection", "cancel_selection", "card-select",
                                      "Close the unavailable deck-card preview.", {"action": "cancel_selection"}, False))
        else:
            _add_option_actions(out, entries=_items(selection.get("cards")), prefix="deck-card",
                                action_name="select_card", index_key="index", phase="card-select",
                                description=lambda x: f"Select {x.get('name', 'card')}: {x.get('description', '')}")
            if selection.get("can_confirm") is True:
                out.append(GameAction("confirm-deck-selection", "confirm_selection", "card-select",
                                      "Confirm selected deck cards.", {"action": "confirm_selection"}, True))
            if selection.get("can_cancel") is True:
                out.append(GameAction("cancel-deck-selection", "cancel_selection", "card-select",
                                      "Cancel this deck-card selection.", {"action": "cancel_selection"}, True))

    # Bundle and Crystal Sphere mini-game mechanics need screen-specific strategy.
    # They intentionally remain unsupported until their complete action contract is implemented.
    return out, errors
